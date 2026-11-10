import os
import tempfile
import unittest
from caplab.maestro_runtime import clone_setup_without_history, effective_runtime, normalize_overrides, selected_test, storage_apply_skill, runtime_apply_skill


class RuntimeTest(unittest.TestCase):
    def test_scope_and_override(self):
        runtime = {'source': {'tests': [{'name':'a'}, {'name':'b'}]}, 'source_test':'b',
                   'snapshot': {'global': {'available':True, 'properties':{'maxjobs':'9'}},
                                'tests':[{'name':'b', 'available':True, 'properties':{'maxjobs':'3'}}]},
                   'overrides': {'maxjobs':'2'}}
        row = effective_runtime(runtime)['maxjobs']
        self.assertEqual((row['original'], row['effective'], row['source'], row['verified']), ('3','2','test',False))
        del runtime['source_test']
        with self.assertRaises(ValueError): selected_test(runtime)

    def test_invalid_overrides(self):
        for value in ({'maxjobs':0}, {'runtimeout':'0'}, {'command':'x\ny'}, {'bad':'x'}):
            with self.assertRaises(ValueError): normalize_overrides(value)

    def test_copy_history_only(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, 'source'); os.mkdir(source)
            for name in ('maestro.sdb','active.state'):
                with open(os.path.join(source, name),'w') as stream:
                    stream.write('<root><option>keep</option><history><historyentry>old</historyentry></history></root>')
            with open(os.path.join(source, 'master.tag'),'w') as stream: stream.write('original')
            os.mkdir(os.path.join(source, 'results'))
            os.mkdir(os.path.join(source, 'test_states'))
            with open(os.path.join(source, 'results', 'history.bin'), 'wb') as stream: stream.write(b'history')
            with open(os.path.join(source, 'test_states', 'old.state'), 'w') as stream: stream.write('history')
            target = os.path.join(root, 'private')
            self.assertEqual(clone_setup_without_history(source, target), 2)
            self.assertFalse(os.path.exists(os.path.join(target, 'results')))
            self.assertFalse(os.path.exists(os.path.join(target, 'test_states')))
            self.assertTrue(os.path.exists(os.path.join(source, 'results', 'history.bin')))
            with open(os.path.join(source, 'maestro.sdb')) as stream: self.assertIn('historyentry', stream.read())
            with open(os.path.join(target, 'maestro.sdb')) as stream:
                data = stream.read()
            self.assertNotIn('historyentry', data)
            self.assertIn('<option>keep</option>', data)

    def test_source_home_and_cyclic_fallback(self):
        runtime = {'source': {'workspace': '/work', 'tests': []}, 'snapshot': {'storage': {
            'source_home': {'available': True, 'value': '/source-home'},
            'project_dir': {'available': True, 'value': '~/simulation'},
            'fallback': {'available': True, 'value': 'RdbDir'}}}}
        code = storage_apply_skill(runtime)
        self.assertIn('/source-home/simulation', code)
        self.assertIn("'cyclic \"RdbDir\"", code)

    def test_job_limit_test_name_mapping(self):
        runtime = {'source': {'tests': [{'name': 'source-test'}]}, 'source_test': 'source-test',
                   'snapshot': {'run_options': {'available': True, 'RunIn': 'Series',
                                 'JobLimit': [['source-test', 3], ['ICRP', 0]]}}}
        code = runtime_apply_skill(runtime, 'CAP_SP', '/tmp/report', 'abc')
        self.assertIn('list(\"CAP_SP\" 3)', code)
        self.assertIn('list(\"ICRP\" 0)', code)

if __name__ == '__main__': unittest.main()
