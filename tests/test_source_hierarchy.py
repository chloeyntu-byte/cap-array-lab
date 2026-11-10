import unittest
import os
import tempfile
from pathlib import Path
import subprocess
import threading
import time
import re
from unittest import mock
from caplab.source_hierarchy import parse_records, scan_source_hierarchy, placement_binding, projected_binding_skill
from caplab.native_viewer import ConfigViewer


class HierarchyRecordsTest(unittest.TestCase):
    def test_occurrence_paths_and_stop_binding_are_preserved(self):
        source = dict(design_library='L', design_cell='C', design_view='config')
        fields = ['"/A"', '"A"', '"/"', '"L"', '"C"', '"symbol"',
                  '"L"', '"TOP"', '"schematic"', '"L"', '"C"', '"extracted"',
                  '"L"', '"C"', '"extracted"', 't', '"L"', '"TOP"',
                  '"config"', '"/"', '"/A"', 't', 't', 'nil', '"schematic"',
                  'nil', 'nil', 'nil', 'nil']
        first = 'CONFIG_BINDING\t' + '\t'.join(fields)
        fields[0] = '"/B"'; fields[1] = '"B"'; fields[20] = '"/B"'
        second = 'CONFIG_BINDING\t' + '\t'.join(fields)
        parsed = parse_records('RESOLVED_TOP\t"L"\t"TOP"\t"schematic"\n'+first+'\n'+second, source)
        self.assertEqual([x['path'] for x in parsed['occurrences']], ['/A','/B'])
        self.assertEqual(parsed['occurrences'][0]['view'], 'extracted')
        self.assertTrue(parsed['occurrences'][0]['stop'])
        self.assertEqual(parsed['errors'], [])

    def test_failures_are_exposed(self):
        result = parse_records('CONFIG_BINDING\tbroken\nCONFIG_CYCLE\t"/A"\t"cycle"', {})
        self.assertEqual([x['kind'] for x in result['errors']], ['CONFIG_RECORD_ERROR','CONFIG_CYCLE'])

    def test_incomplete_design_is_rejected(self):
        with self.assertRaises(ValueError): scan_source_hierarchy('/tmp', {})

    def test_projection_keeps_child_paths_and_actual_binding(self):
        root = dict(path='/A', parent='/', name='A', raw_library='symbol_lib', raw_cell='symbol_cell',
                    raw_view='symbol', library='impl_lib', cell='impl_cell', view='schematic', traversable=True)
        child = dict(path='/A/X<3:0>', parent='/A', name='X<3:0>', raw_library='lib', raw_cell='dev',
                     raw_view='symbol', library='lib', cell='dev', view='extracted', stop=True)
        ignored = dict(path='/A/P', parent='/A', name='P', ignored=True)
        graph = dict(occurrences=[root, child, ignored], errors=[])
        result = placement_binding(graph, root, 'DUT')
        result['id'] = 'DUT'
        self.assertEqual((result['library'], result['binding_library']), ('symbol_lib', 'impl_lib'))
        self.assertEqual([x['path'] for x in result['binding_records']], ['/A', '/A/X<3:0>'])
        text = projected_binding_skill({'instances':[result]}, 'private', 'top')
        self.assertIn('"private" "top" "schematic" "DUT"', text)
        self.assertIn('"impl_lib" "impl_cell" "schematic" "X<3:0>"', text)
        self.assertIn('"lib" "dev" "extracted"', text)
        self.assertIn("hdbcStopRule t", text)
        self.assertIn('Projected binding readback mismatch', text)

    def test_partial_source_cannot_be_projected(self):
        with self.assertRaises(ValueError):
            placement_binding({'errors':[{'kind':'CONFIG_CYCLE'}]}, {}, 'DUT')

    def test_scan_forwards_native_context_and_explicit_mapping(self):
        source = dict(design_library='L', design_cell='C', design_view='config')
        snapshot = {'PATH': {'state':'value', 'value':'/source/bin'}}
        with tempfile.TemporaryDirectory() as folder:
            mapping = Path(folder) / 'other.lib'; mapping.write_text('DEFINE L /tmp\n')
            with mock.patch('caplab.source_hierarchy.subprocess.run', side_effect=subprocess.TimeoutExpired('virtuoso', 1)) as run:
                with self.assertRaises(RuntimeError):
                    scan_source_hierarchy(folder, source, timeout=1, native_environment=snapshot,
                                          native_virtuoso='/source/virtuoso', cds_lib=str(mapping))
                kwargs=run.call_args[1]
                self.assertEqual(kwargs['env']['CAPLAB_NATIVE_VIRTUOSO'], '/source/virtuoso')
                self.assertIn('/source/bin', kwargs['env']['CAPLAB_NATIVE_ENV_JSON'])
                self.assertNotEqual(kwargs['cwd'], folder)

    def test_missing_invoking_executable_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'cds.lib').write_text('')
            source=dict(design_library='L', design_cell='C', design_view='config')
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, 'Invoking Virtuoso'):
                    scan_source_hierarchy(folder, source, native_environment={'PATH':{}})

    def test_scan_ignores_global_native_executable_and_clears_standalone_environment(self):
        source=dict(design_library='L', design_cell='C', design_view='config')
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'cds.lib').write_text('')
            with mock.patch.dict(os.environ, {'CAPLAB_NATIVE_VIRTUOSO':'/other/virtuoso', 'CAPLAB_NATIVE_ENV_JSON':'stale'}):
                with self.assertRaisesRegex(ValueError, 'Invoking Virtuoso'):
                    scan_source_hierarchy(folder,source,native_environment={'PATH':{}})
                with mock.patch('caplab.source_hierarchy.subprocess.run', side_effect=subprocess.TimeoutExpired('virtuoso',1)) as run:
                    with self.assertRaises(RuntimeError): scan_source_hierarchy(folder,source,timeout=1)
                    env=run.call_args[1]['env']
                    self.assertNotIn('CAPLAB_NATIVE_ENV_JSON',env)
                    self.assertNotIn('CAPLAB_NATIVE_VIRTUOSO',env)

    def test_viewer_no_global_executable_fallback_and_standalone_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {'DISPLAY':':99','CAPLAB_NATIVE_VIRTUOSO':'/other/virtuoso','CAPLAB_NATIVE_ENV_JSON':'stale'}):
                viewer=ConfigViewer()
                with self.assertRaisesRegex(ValueError,'Invoking Virtuoso'):
                    viewer.open(folder,'L','C',native_environment={'PATH':{}})
                def spawn(args, **kwargs):
                    request=Path(args[2]).parent/'request.il'
                    ack=re.search(r'calAck=outfile\("([^"\n]+)"\)',request.read_text()).group(1)
                    Path(ack).write_text('OK')
                    return mock.Mock(poll=mock.Mock(return_value=None))
                with mock.patch('caplab.native_viewer.ROOT',Path(folder)), mock.patch('caplab.native_viewer.subprocess.Popen',side_effect=spawn) as popen:
                    viewer.open(folder,'L','C')
                    env=popen.call_args[1]['env']
                    self.assertNotIn('CAPLAB_NATIVE_ENV_JSON',env)
                    self.assertNotIn('CAPLAB_NATIVE_VIRTUOSO',env)

    def test_scan_uses_invoking_workdir_and_current_mapping(self):
        source=dict(design_library='L', design_cell='C', design_view='config')
        with tempfile.TemporaryDirectory() as root:
            workspace=Path(root)/'mapping';workspace.mkdir();(workspace/'cds.lib').write_text('')
            workdir=Path(root)/'invoking';workdir.mkdir()
            with mock.patch.dict(os.environ, {'CAPLAB_NATIVE_CDS_LIB':'/stale'}):
                with mock.patch('caplab.source_hierarchy.subprocess.run', side_effect=subprocess.TimeoutExpired('virtuoso',1)) as run:
                    with self.assertRaises(RuntimeError):
                        scan_source_hierarchy(str(workspace),source,timeout=1,native_workdir=str(workdir))
                    self.assertEqual(run.call_args[1]['cwd'],str(workdir))
                    self.assertEqual(run.call_args[1]['env']['CAPLAB_NATIVE_CDS_LIB'],str(workspace/'cds.lib'))
                    with self.assertRaises(RuntimeError):scan_source_hierarchy(str(workspace),source,timeout=1)
                    self.assertNotIn('CAPLAB_NATIVE_CDS_LIB',run.call_args[1]['env'])

    def test_viewer_workdir_and_mapping_change_session_identity(self):
        with tempfile.TemporaryDirectory() as root:
            workspace=Path(root)/'mapping';workspace.mkdir();(workspace/'cds.lib').write_text('')
            first=Path(root)/'first';first.mkdir();second=Path(root)/'second';second.mkdir()
            def spawn(args, **kwargs):
                request=Path(args[2]).parent/'request.il'
                ack=re.search(r'calAck=outfile\("([^"\n]+)"\)',request.read_text()).group(1)
                Path(ack).write_text('OK')
                return mock.Mock(poll=mock.Mock(return_value=None))
            with mock.patch.dict(os.environ, {'DISPLAY':':99','CAPLAB_NATIVE_CDS_LIB':'/stale'}):
                with mock.patch('caplab.native_viewer.ROOT',Path(root)), mock.patch('caplab.native_viewer.subprocess.Popen',side_effect=spawn) as popen:
                    viewer=ConfigViewer()
                    viewer.open(str(workspace),'L','C',native_workdir=str(first))
                    self.assertEqual(popen.call_args[1]['cwd'],str(first))
                    self.assertEqual(popen.call_args[1]['env']['CAPLAB_NATIVE_CDS_LIB'],str(workspace/'cds.lib'))
                    viewer.open(str(workspace),'L','C',native_workdir=str(second))
                    self.assertEqual(popen.call_count,2)
                    viewer.open(str(workspace),'L','C')
                    self.assertNotIn('CAPLAB_NATIVE_CDS_LIB',popen.call_args[1]['env'])

    def test_viewer_serializes_concurrent_requests(self):
        viewer=ConfigViewer(); active=[]; overlap=[]; started=threading.Event()
        def operation(*args):
            active.append(1)
            if len(active)>1: overlap.append(True)
            started.set();time.sleep(.05);active.pop()
        with mock.patch.object(viewer,'_open',side_effect=operation):
            first=threading.Thread(target=viewer.open,args=('/tmp','L','A'))
            second=threading.Thread(target=viewer.open,args=('/tmp','L','B'))
            first.start(); self.assertTrue(started.wait(1));second.start()
            first.join(2);second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(overlap,[])

    def test_viewer_rejects_unsupported_mode(self):
        with self.assertRaises(ValueError): ConfigViewer().open('/tmp','L','C',mode='w')

if __name__ == '__main__': unittest.main()
