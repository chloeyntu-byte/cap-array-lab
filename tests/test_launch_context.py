import os
import tempfile
import unittest
from caplab.launch_context import ENV_NAMES, child_environment, load_context, parse_records


def records(cell='A', value='x'):
    rows = ['CAPLAB_MAESTRO_CONTEXT_1', 'C\t"lib"\t"%s"\t"maestro"\t"/work"\t"/lib/%s/maestro"\tnil\t"/bin/ocean"' % (cell, cell),
            'M\t"unknown"', 'T\t"test"\t"lib"\t"%s"\t"config"\t"spectre"' % cell,
            'P\t"global"\tnil\tt', 'J\t"global"\tnil\t"maxjobs"\t"4"',
            'P\t"test"\t"test"\tt', 'D\t"test"\t"test"',
            'Q\t"distributed"', 'U\t"RunIn"\tt\t"LSF"', 'V\t"save_dir"\tt\t"/tmp/out"']
    for name in ENV_NAMES:
        val = 'nil' if name == 'LD_LIBRARY_PATH' else '""' if name == 'PYTHONPATH' else '"%s"' % value
        rows.append(('R\t' + val) if name == 'LD_LIBRARY_PATH' else ('E\t"%s"\t%s' % (name, val)))
    return '\n'.join(rows) + '\n'


class LaunchContextTests(unittest.TestCase):
    def test_per_window_and_policy(self):
        a, b = parse_records(records('A')), parse_records(records('B', 'other'))
        self.assertEqual(a['source']['tests'][0]['design_cell'], 'A')
        self.assertEqual(b['source']['tests'][0]['design_cell'], 'B')
        self.assertIsNone(a['source']['modified'])
        self.assertEqual(a['snapshot']['tests'][0]['properties']['maxjobs'], '4')
        self.assertEqual(a['native_environment']['LD_LIBRARY_PATH']['state'], 'unset')
        self.assertEqual(a['native_environment']['PYTHONPATH']['state'], 'empty')
        self.assertNotEqual(a['native_environment']['PATH'], b['native_environment']['PATH'])
    def test_native_environment_replaces_injected_gui_values(self):
        context = parse_records(records())
        base = dict((key, 'injected') for key in ENV_NAMES)
        base['DISPLAY'] = ':94'
        env = child_environment(base, context)
        self.assertNotIn('LD_LIBRARY_PATH', env)
        self.assertEqual(env['PYTHONPATH'], '')
        self.assertEqual(env['PATH'], 'x')
        self.assertEqual(env['DISPLAY'], ':94')
    def test_private_file_and_unsafe_record(self):
        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, records().encode()); os.close(fd)
            self.assertEqual(load_context(path)['source']['cell'], 'A')
            os.chmod(path, 0o644)
            with self.assertRaises(ValueError): load_context(path)
            with self.assertRaises(ValueError): parse_records(records() + 'C\t(evil)\n')
        finally:
            os.unlink(path)

if __name__ == '__main__': unittest.main()
