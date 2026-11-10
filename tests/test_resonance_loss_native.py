import unittest

from caplab.resonance import find_first_resonance
from caplab.loss_native import loss_percentages, terminal_power


def row(f, y):
    out = {'frequency_Hz': f}
    for pair, value in [('11', y), ('12', -y), ('21', -y), ('22', y)]:
        out['Y{}_G_S'.format(pair)] = value.real
        out['Y{}_B_S'.format(pair)] = value.imag
    return out


class ResonanceLossTest(unittest.TestCase):
    def test_resonance_interpolates_admittance(self):
        result = find_first_resonance([row(1, 1-2j), row(3, 1+2j)])
        self.assertTrue(result['available'])
        self.assertAlmostEqual(result['frequency_Hz'], 2)
        self.assertAlmostEqual(result['Z_real_ohm'], 1)
        self.assertEqual(result['kind'], 'parallel-like')
        self.assertEqual(result['bracket_Hz'], [1, 3])

    def test_no_crossing_or_admittance_pole(self):
        self.assertFalse(find_first_resonance([row(1, 1+1j), row(2, 1+2j)])['available'])
        self.assertFalse(find_first_resonance([row(1, -1j), row(2, 1j)])['available'])

    def test_real_multiterminal_power_and_balance(self):
        # Arbitrary reference: four-terminal dissipative block with two ports.
        # Each port is a resistor, so P = .5*(|V1-V2|^2/R1 + |V3-V4|^2/R2).
        terminals = [dict(voltage=3+1j, current=1+0j),
                     dict(voltage=1+1j, current=-1+0j),
                     dict(voltage=2+2j, current=0+1j),
                     dict(voltage=2+0j, current=0-1j)]
        self.assertAlmostEqual(terminal_power(terminals), 2)
        result = loss_percentages({'multiport': terminals,
            'other': [dict(voltage=2, current=1), dict(voltage=0, current=-1)]}, 3, complete=True)
        self.assertTrue(result['available'])
        self.assertAlmostEqual(result['modules']['multiport'], 200/3)
        self.assertAlmostEqual(result['modules']['other'], 100/3)

    def test_incomplete_unbalanced_and_negative_are_withheld(self):
        data = {'a': [dict(voltage=2, current=1)]}
        self.assertFalse(loss_percentages(data, 1)['available'])
        self.assertFalse(loss_percentages(data, 2, complete=True)['available'])
        data['coupled'] = [dict(voltage=2, current=-0.2)]
        self.assertFalse(loss_percentages(data, 0.8, complete=True)['available'])


class NativeRecordsTest(unittest.TestCase):
    def test_complete_records_and_endpoint_power_interpolation(self):
        from caplab.loss_native import parse_records, evaluate_sample
        plan = {'probes': [{'instance': 'R', 'pin': 'P'}]}
        lines = ['LOSS_INPUT\t1\t2\t0\t0\t0', 'LOSS_PIN\t0\t1\t2\t0\t1\t0',
                 'LOSS_INPUT\t3\t4\t0\t0\t0', 'LOSS_PIN\t0\t3\t4\t0\t1\t0']
        rows = parse_records(lines, plan)
        result = evaluate_sample(rows, plan, 2)
        self.assertTrue(result['available'])
        self.assertEqual(result['input_power_W'], 1.5)
        self.assertEqual(result['modules']['R'], 100)
        self.assertTrue(result['estimated'])
        with self.assertRaises(ValueError):
            parse_records(lines[:-1], plan)

class FloatingGaugeTest(unittest.TestCase):
    def test_free_common_mode(self):
        from caplab.loss_native import evaluate_sample
        plan = {'port_fixture': 'ground_50ohm_each', 'probes': [
            {'instance': 'R', 'pin': 'PLUS'}, {'instance': 'R', 'pin': 'MINUS'}]}
        r = {'frequency_Hz': 1e9, 'input': [25, 0, -25, 0],
             'pins': {'0': [25, 0, .5, 0], '1': [-25, 0, -.5, 0]},
             'cm_input': [37.5, 0, 12.5, 0],
             'cm_pins': {'0': [37.5, 0, .25, 0], '1': [12.5, 0, -.25, 0]}}
        result = evaluate_sample([r], plan, 1e9)
        self.assertTrue(result['available'], result)
        self.assertAlmostEqual(result['input_power_W'], 50)
        self.assertAlmostEqual(result['modules']['R'], 100)
        r['input'][0] += 1
        self.assertFalse(evaluate_sample([r], plan, 1e9)['available'])

if __name__ == '__main__':
    unittest.main()
