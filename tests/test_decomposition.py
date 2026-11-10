"""Circuit-level checks for floating-port capacitance and loss decomposition."""
import json
import math
import unittest
from caplab.decomposition import decompose, result_names, MEASUREMENT_KEYS


def pi_matrix(mutual, p_ground, n_ground):
    return mutual + p_ground, -mutual, -mutual, mutual + n_ground


class DecompositionTests(unittest.TestCase):
    def test_asymmetric_capacitors_add_at_floating_port(self):
        f = 1e9; w = 2 * math.pi * f
        result = decompose(*pi_matrix(1j*w*1e-12, 1j*w*1e-12, 1j*w*3e-12), frequency=f)
        self.assertTrue(result['valid'], result)
        for key, expected in [('coupling', 1e-12), ('ground', .75e-12), ('total', 1.75e-12), ('p_ground', 1e-12), ('n_ground', 3e-12)]:
            self.assertAlmostEqual(result[key]['C_F'] / expected, 1, places=12)
            self.assertIsNone(result[key]['Q'])
        self.assertAlmostEqual(result['coupling']['C_F'] + result['ground']['C_F'], result['total']['C_F'], delta=1e-27)
        # Clamping the common-mode voltage gives 2 pF, a different condition.
        clamped = 1e-12 + (1e-12 + 3e-12) / 4
        self.assertAlmostEqual(clamped / 1e-12, 2)
        self.assertNotAlmostEqual(result['total']['C_F'] / 1e-12, clamped / 1e-12)

    def test_lossy_ground_path_uses_complex_series_and_q_does_not_add(self):
        f = 2e9; w = 2 * math.pi * f
        ym = 1/5000. + 1j*w*1e-12
        yp = 1/1000. + 1j*w*2e-12
        yn = 1/3000. + 1j*w*5e-12
        result = decompose(*pi_matrix(ym, yp, yn), frequency=f)
        self.assertTrue(result['valid'], result)
        expected_ground = 1 / (1/yp + 1/yn)
        expected_total = ym + expected_ground
        for name, value in [('coupling', ym), ('ground', expected_ground), ('total', expected_total)]:
            self.assertAlmostEqual(result[name]['G_S'] / value.real, 1, places=12)
            self.assertAlmostEqual(result[name]['B_S'] / value.imag, 1, places=12)
            self.assertAlmostEqual(result[name]['Q'], value.imag/value.real, places=10)
        self.assertNotAlmostEqual(result['total']['Q'], result['coupling']['Q'] + result['ground']['Q'])
        naive_c = 2e-12 * 5e-12 / (2e-12 + 5e-12)
        self.assertGreater(abs(result['ground']['C_F'] - naive_c), 1e-18)

    def test_nonreciprocal_total_retained_but_physical_split_withheld(self):
        f = 1e9; k = 1j * 2 * math.pi * f * 1e-12
        result = decompose(2*k, -k, -.8*k, 3*k, f)
        self.assertFalse(result['valid'])
        self.assertIn('Non-reciprocal', result['message'])
        self.assertIsNone(result['approximate'])
        self.assertAlmostEqual(result['total']['C_F']/1e-12, 1.625, places=12)
        for key in ('coupling', 'ground', 'p_ground', 'n_ground'):
            self.assertIsNone(result[key])

    def test_pure_floating_cross_cap_has_zero_ground_not_zero_total(self):
        f = 1e9; y = 1j*2*math.pi*f*1e-12
        result = decompose(y, -y, -y, y, f)
        self.assertTrue(result['valid'], result)
        self.assertAlmostEqual(result['total']['C_F']/1e-12, 1)
        self.assertEqual(result['ground']['C_F'], 0)
        self.assertIsNone(result['ground']['Q'])
        empty = decompose(0, 0, 0, 0, f)
        self.assertTrue(empty['valid'])
        self.assertEqual(empty['total']['C_F'], 0)
        self.assertIsNone(empty['total']['Q'])

    def test_resolved_tiny_ground_is_not_clipped_by_mutual_scale(self):
        f = 1e9; w = 2*math.pi*f
        result = decompose(*pi_matrix(1j*w*1e-12, 1j*w*1e-24, 1j*w*3e-24), frequency=f)
        self.assertTrue(result['valid'], result)
        self.assertGreater(result['ground']['C_F'], 0)
        self.assertAlmostEqual(result['ground']['C_F']/.75e-24, 1, delta=2e-4)

    def test_cancelling_ground_return_is_explicitly_unavailable(self):
        f = 1e9; y = 1j*2*math.pi*f*1e-12
        result = decompose(*pi_matrix(y, y, -y), frequency=f)
        self.assertFalse(result['valid'])
        self.assertIsNone(result['total'])
        self.assertIn('Singular', result['message'])
        self.assertIsNone(result['ground'])

    def test_negative_effective_loss_does_not_become_passive_q(self):
        f = 1e9; w = 2*math.pi*f
        result = decompose(*pi_matrix(-.001 + 1j*w*1e-12, .004 + 1j*w*2e-12, .004 + 1j*w*2e-12), frequency=f)
        self.assertTrue(result['valid'], result)
        self.assertIsNone(result['coupling']['Q'])
        self.assertLess(result['coupling']['Q_signed'], 0)
        self.assertLess(result['coupling']['G_S'], 0)
        self.assertGreater(result['coupling']['C_F'], 0)
        self.assertIn('Negative effective conductance', result['coupling']['status'])
        self.assertGreater(result['total']['Q'], 0)
        self.assertAlmostEqual(result['total']['C_F']/1e-12, 2)

    def test_negative_capacitance_keeps_sign(self):
        f = 1e9; y = -1j*2*math.pi*f*1e-12
        result = decompose(y, -y, -y, y, f)
        self.assertTrue(result['valid'])
        self.assertLess(result['total']['C_F'], 0)
        self.assertIn('Non-capacitive', result['total']['status'])

    def test_small_reciprocity_roundoff_symmetrizes_consistently(self):
        f = 1e9; w = 2*math.pi*f
        a,b,c,d = pi_matrix(.001 + 1j*w*1e-12, .002 + 1j*w*2e-12, .003 + 1j*w*3e-12)
        result = decompose(a, b*(1+1e-9), c*(1-1e-9), d, f)
        self.assertTrue(result['valid'], result)
        self.assertGreater(result['reciprocity_error'], 0)
        self.assertIn('roundoff_symmetrized', result['flags'])
        self.assertAlmostEqual(result['coupling']['C_F'] + result['ground']['C_F'], result['total']['C_F'], delta=1e-27)
        self.assertLess(result['reconstruction_error'], 1e-12)

    def test_interpolate_matrix_before_nonlinear_ground_reduction(self):
        def matrix(f):
            w = 2*math.pi*f
            return pi_matrix(.0002+1j*w*1e-12, .001+1j*w*1e-12, .001+1j*w*3e-12)
        low, high = matrix(1e9), matrix(3e9)
        interpolated = tuple((a+b)/2 for a,b in zip(low, high))
        result = decompose(*interpolated, frequency=2e9)
        actual = decompose(*matrix(2e9), frequency=2e9)
        self.assertTrue(result['valid'])
        self.assertAlmostEqual(result['ground']['C_F']/actual['ground']['C_F'], 1, places=13)
        wrong = (decompose(*low, frequency=1e9)['ground']['C_F'] + decompose(*high, frequency=3e9)['ground']['C_F']) / 2
        self.assertGreater(abs(result['ground']['C_F'] - wrong), 1e-18)

    def test_invalid_inputs_and_overflow_are_json_safe(self):
        for values in [(1,2,3,4,0), (1,2,3,4,float('nan')), (1,2,float('inf'),4,1e9), (None,0,0,0,1e9), (1e308j,-1e308j,-1e308j,1e308j,1e-300)]:
            result = decompose(*values)
            self.assertFalse(result['valid'], result)
            json.dumps(result, allow_nan=False)
        json.dumps(decompose(*pi_matrix(.1+.2j, .3+.4j, .5+.6j), frequency=1e9), allow_nan=False)

    def test_weak_nonreciprocity_has_explicit_fit_without_changing_strict_validity(self):
        f = 1e9; k = 1j*2*math.pi*f*1e-12
        a,b,c,d = 2*k, -(1+1e-5)*k, -(1-1e-5)*k, 3*k
        result = decompose(a,b,c,d,f)
        self.assertFalse(result['valid'])
        for name in ('coupling','ground','p_ground','n_ground'):
            self.assertIsNone(result[name])
        exact = (a*d-b*c)/(a+b+c+d)
        self.assertAlmostEqual(result['total']['B_S']/exact.imag, 1, places=12)
        fit = result['approximate'];self.assertIsNotNone(fit)
        self.assertEqual(fit['method'], 'Symmetric reciprocal pi fit')
        self.assertLess(fit['reconstruction_error'], 1e-6)
        self.assertGreater(abs(fit['correction']['C_F']), 0)
        self.assertAlmostEqual((fit['coupling']['C_F']+fit['ground']['C_F']+fit['correction']['C_F'])/result['total']['C_F'], 1, places=13)
        self.assertAlmostEqual(fit['c_sum_residual_F'], -fit['correction']['C_F'], delta=1e-27)
        json.dumps(result,allow_nan=False)

    def test_weak_matrix_error_with_bad_floating_reconstruction_has_no_fit(self):
        f = 1e9;k=1j*2*math.pi*f*1e-12
        result=decompose((1+1e-6)*k,(-1+1e-5)*k,(-1-1e-5)*k,(1+1e-6)*k,f)
        self.assertFalse(result['valid'])
        self.assertLess(result['reciprocity_error'],1e-4)
        self.assertIsNone(result['approximate'])
        self.assertIsNotNone(result['total'])

    def test_strict_reciprocity_roundoff_does_not_replace_exact_floating_total(self):
        f=1e9;k=1j*2*math.pi*f*1e-12
        shunt=1e-4;delta=3e-7
        result=decompose((1+shunt)*k,(-1+delta)*k,(-1-delta)*k,(1+shunt)*k,f)
        self.assertTrue(result['valid'],result)
        # Closed-form exact total includes the small nonreciprocal correction,
        # even though the original strict roundoff tolerance accepts the matrix.
        expected=(1+shunt/2+delta*delta/(2*shunt))*1e-12
        self.assertAlmostEqual(result['total']['C_F']/expected,1,places=14)
        self.assertGreater(abs(result['additivity_error_F']),1e-23)
        self.assertIsNone(result['approximate'])

    def test_real_array_matrix_fit_keeps_negative_effective_loss_diagnostic(self):
        # Actual complete native BANK=0 / 1 GHz observation, retained literally
        # so this numerical regression does not require a PDK or result folder.
        result=decompose(complex(7.357352735076558e-5,.008476824692277549),
                         complex(5.903503717280419e-5,-.00036011349591261567),
                         complex(5.901712815886237e-5,-.0003601133534391621),
                         complex(7.350019995784635e-5,.008476826294037416),1e9)
        self.assertFalse(result['valid'])
        self.assertGreater(result['reciprocity_error'],1e-6)
        fit=result['approximate'];self.assertIsNotNone(fit)
        self.assertIsNone(fit['coupling']['Q'])
        self.assertLess(fit['coupling']['Q_signed'],0)
        self.assertGreater(result['total']['Q'],0)
        self.assertGreater(fit['ground']['Q'],0)
        self.assertAlmostEqual(fit['coupling']['C_F']*1e15,57.31383161091863,places=8)
        self.assertAlmostEqual(fit['ground']['C_F']*1e15,645.9074236894227,places=8)
        self.assertLess(abs(fit['c_sum_residual_F'])*1e15,1e-8)
        self.assertLess(fit['g_reconstruction_relative_error'],1e-8)
        self.assertIsNone(decompose(*pi_matrix(.001+.002j,.003+.004j,.005+.006j),frequency=1e9)['approximate'])

    def test_register_names_exclude_all_measurement_payloads(self):
        row = dict(point=0, code=3, frequency_Hz=1e9, COARSE=2, FINE=3, C_F=1e-12, Q=20, G_S=.1, B_S=.2, status='Capacitive', decomposition={'schema':1})
        row.update({key:0 for key in MEASUREMENT_KEYS if key.startswith('Y')})
        self.assertEqual(result_names(row), ['COARSE','FINE'])


if __name__ == '__main__':
    unittest.main()
