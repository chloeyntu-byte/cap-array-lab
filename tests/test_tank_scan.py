"""Tank scan analysis against an analytic cross-coupled LC network.

The synthetic records use the exact native format (two raw excitations, all
module terminals probed, 50-ohm ground-referenced ports), so the floating
reconstruction, module admittances and gm split are checked end to end.
"""
import cmath
import math
import unittest

from caplab import tank_scan

from caplab.tank_example import (module_currents, records as _records, analytic as _analytic,
                                  exact_f0 as _exact_f0, GM, GDS, CG, G_ARR, C_UNIT)

C_ARR = C_UNIT * 4      # code 2


def records(freqs, gm=GM, currents=module_currents):
    return _records(freqs, 2, gm, currents)


def analytic(f):
    return _analytic(f, 2)


def exact_f0(gm_present=True):
    return _exact_f0(2, gm_present)


ROLES = {'IND': 'inductor', 'ARR': 'array', 'GM': 'gm'}


class TankScanAnalytic(unittest.TestCase):
    def setUp(self):
        self.f0 = exact_f0()
        freqs = [10e6] + [self.f0 * (0.9 + 0.2 * k / 400) for k in range(401)]
        self.plan, self.loss, self.spectrum = records(freqs)

    def test_active_startup_values(self):
        r = tank_scan.analyze_point(self.spectrum, self.loss, self.plan, ROLES, 'active')
        self.assertTrue(r['available'], r.get('reason'))
        a = analytic(self.f0)
        self.assertAlmostEqual(r['f0_Hz'] / self.f0, 1, places=6)
        self.assertAlmostEqual(r['G_neg_S'] / (GM / 2), 1, places=5)
        self.assertAlmostEqual(r['G_core_load_S'] / (GDS / 2), 1, places=5)
        g_lc = a['ind'].real + a['arr'].real
        self.assertAlmostEqual(r['Rp_LC_ohm'] * g_lc, 1, places=5)
        g_loss = g_lc + GDS / 2
        self.assertAlmostEqual(r['Rp_loaded_ohm'] * g_loss, 1, places=5)
        self.assertAlmostEqual(r['margin'] / ((GM / 2) / g_loss), 1, places=5)
        self.assertAlmostEqual(r['gm_ref_S'] / GM, 1, places=9)
        self.assertAlmostEqual(r['gds_ref_S'] / GDS, 1, places=9)
        self.assertAlmostEqual(r['gmRp'] / (GM / g_loss), 1, places=5)
        self.assertLess(abs(r['net_check_S']), 1e-6 * g_loss)
        self.assertLess(r['module_sum_error'], 1e-9)
        # LC-only resonance excludes the gate capacitance of the core.
        self.assertAlmostEqual(r['f0_LC_Hz'] / exact_f0(False), 1, places=5)
        self.assertGreater(r['f0_LC_Hz'], r['f0_Hz'])
        shares = {c['module']: c['share_pct'] for c in r['contributions']}
        self.assertAlmostEqual(sum(shares.values()), 100, places=6)
        self.assertAlmostEqual(shares['GM'], 100 * (GDS / 2) / g_loss, places=4)
        self.assertTrue(r['shares_valid'])
        # Effective C and Q of the loaded tank.
        c_total = C_ARR + CG / 2
        self.assertAlmostEqual(r['C_eff_F'] / c_total, 1, delta=0.03)

    def test_passive_mode_matches_re_z(self):
        roles = dict(ROLES, GM='load')
        r = tank_scan.analyze_point(self.spectrum, self.loss, self.plan, roles, 'passive')
        self.assertTrue(r['available'], r.get('reason'))
        # Without the gm split the core enters as a net (negative) load.
        a = analytic(self.f0)
        self.assertAlmostEqual(r['G_loss_S'] / (a['ind'].real + a['arr'].real + (GDS - GM) / 2), 1, places=5)
        self.assertNotIn('margin', r)

    def test_passive_lc_only(self):
        plan, loss, spectrum = records([1e9 + k * 0.1e9 for k in range(400)], gm=0.0)
        r = tank_scan.analyze_point(spectrum, loss, plan, {'IND': 'inductor', 'ARR': 'array', 'GM': 'load'}, 'passive')
        self.assertTrue(r['available'], r.get('reason'))
        self.assertAlmostEqual(r['Rp_loaded_ohm'] / r['Z_real_ohm'], 1, places=4)

    def test_roles_and_mode(self):
        project = {'registers': [{'roles': {'B<0>': 'BANK<0>', 'VSS': 'VSS'}}],
                   'instances': [{'id': 'I0', 'kind': 'snp'}, {'id': 'I1', 'cell': 'CT_x', 'roles': {'SW<0>': 'BANK<0>'}},
                                 {'id': 'I2', 'cell': 'XCOUPLED_GM', 'roles': {'OUTP': 'P'}},
                                 {'id': 'I3', 'kind': 'passive', 'cell': 'capq'}]}
        page = {}
        s = tank_scan.tank_settings(page, project)
        self.assertEqual(s['roles'], {'I0': 'inductor', 'I1': 'array', 'I2': 'gm', 'I3': 'load'})
        self.assertEqual(tank_scan.backend_mode(s), 'active')
        page['tank']['contains']['gm'] = False
        s = tank_scan.tank_settings(page, project)
        self.assertEqual(tank_scan.backend_mode(s), 'passive')
        self.assertEqual(tank_scan.effective_roles(s)['I2'], 'load')
        page['tank']['contains']['gm'] = True
        page['tank']['roles']['I2'] = 'load'
        self.assertTrue(tank_scan.check_roles(tank_scan.tank_settings(page, project)))

    def test_tank_data_rows(self):
        project = {'private': {'spectrum': {'rows': self.spectrum}, 'loss_spectrum': {'2': self.loss},
                               'loss_plan': self.plan},
                   'instances': [{'id': k} for k in ROLES], 'registers': []}
        settings = {'roles': dict(ROLES), 'contains': {'inductor': True, 'array': True, 'gm': True}}
        data = tank_scan.tank_data(project, settings)
        self.assertEqual(data['mode'], 'active')
        self.assertTrue(data['rows'][0]['tank']['available'], data['rows'][0]['tank'].get('reason'))
        sweep = tank_scan.suggested_sweep(data['rows'])
        self.assertLess(sweep['simulation_start'], self.f0)
        self.assertGreater(sweep['simulation_stop'], self.f0)


class TankScanDefinition(unittest.TestCase):
    """gm rolls off with frequency: primary values follow tank-scan (DC gm cancelled)."""
    def test_rolloff(self):
        fp = 20e9
        def rolled(f, vp, vn, code=2, gm=GM):
            return module_currents(f, vp, vn, code, GM / (1 + 1j * f / fp))
        f0 = exact_f0()
        plan, loss, spectrum = records([10e6] + [f0 * (0.8 + 0.4 * k / 800) for k in range(801)], currents=rolled)
        r = tank_scan.analyze_point(spectrum, loss, plan, ROLES, 'active')
        self.assertTrue(r['available'], r.get('reason'))
        f = r['f0_Hz']
        gmf = GM / (1 + 1j * f / fp)
        a = analytic(f)
        g_net = a['ind'].real + a['arr'].real + (GDS - gmf.real) / 2
        dc_gm = GM / (1 + 1j * 10e6 / fp)
        self.assertAlmostEqual(r['G_neg_S'] / (dc_gm.real / 2), 1, places=4)
        self.assertAlmostEqual((r['G_loss_S'] - r['G_neg_S']) / g_net, 1, places=4)
        self.assertAlmostEqual(r['G_neg_f0_S'] / (gmf.real / 2), 1, places=3)
        self.assertGreater(r['G_loss_S'], r['G_loss_f0_S'])
        self.assertIn('tank-scan', r['definition'])


class TankExample(unittest.TestCase):
    def test_bundled_example_analyzes(self):
        import json
        from pathlib import Path
        from caplab.workbook import workbook
        path = Path(__file__).resolve().parents[1] / 'examples' / 'ideal_tank.json'
        page = workbook(json.loads(path.read_text(encoding='utf-8')))['pages'][0]
        settings = tank_scan.tank_settings(page, page['project'])
        data = tank_scan.tank_data(page['project'], settings)
        self.assertEqual(len(data['rows']), 4)
        for row in data['rows']:
            t = row['tank']
            self.assertTrue(t['available'], t.get('reason'))
            self.assertAlmostEqual(t['G_neg_S'] / (GM / 2), 1, places=5)
            self.assertGreater(t['margin'], 1)


class EdgeCases(unittest.TestCase):
    """Regression cases from the v1.1.0 review."""
    def test_reference_point_not_a_resonance_bracket(self):
        f0 = exact_f0()
        plan, loss, spectrum = records([10e6] + [6e9 + 2e9 * k / 200 for k in range(201)])
        r = tank_scan.analyze_point(spectrum, loss, plan, ROLES, 'active', sweep_start=6e9)
        self.assertFalse(r['available'])
        plan, loss, spectrum = records([10e6] + [4e9 + 4e9 * k / 400 for k in range(401)])
        r = tank_scan.analyze_point(spectrum, loss, plan, ROLES, 'active', sweep_start=4e9)
        self.assertTrue(r['available'], r.get('reason'))
        self.assertAlmostEqual(r['f0_Hz'] / f0, 1, places=4)
        self.assertAlmostEqual(r['gm_ref_S'] / GM, 1, places=9)

    def test_f0_on_first_sweep_sample(self):
        plan, loss, spectrum = records([10e6] + [4e9 + 4e9 * k / 400 for k in range(401)])
        orig = tank_scan.find_first_resonance
        tank_scan.find_first_resonance = lambda samples: {'available': True, 'frequency_Hz': 4e9, 'Z_real_ohm': 100.0,
                                                          'Z_imag_ohm': 0.0, 'bracket_Hz': [4e9, 4.01e9]}
        try:
            r = tank_scan.analyze_point(spectrum, loss, plan, ROLES, 'active', sweep_start=4e9)
        finally:
            tank_scan.find_first_resonance = orig
        self.assertTrue(r['available'], r.get('reason'))

    def test_exact_zero_crossings(self):
        self.assertEqual(tank_scan._crossing([1, 2, 3], [0, 1, 2]), (1, 0, 1))
        self.assertEqual(tank_scan._crossing([1, 2, 3], [-2, -1, 0]), (3, 1, 2))
        self.assertIsNone(tank_scan._crossing([1, 2, 3], [1, 2, 3]))


class ReferenceGrid(unittest.TestCase):
    def test_reference_point_prepended(self):
        from caplab.spectrum import simulation_grid
        s = {'frequencies': '5G', 'simulation_start': '4G', 'simulation_stop': '6G', 'simulation_points': 3,
             'simulation_sweep': 'linear', 'tank_gm_ref': '10M'}
        self.assertEqual(simulation_grid(s), [1e7, 4e9, 5e9, 6e9])
        s['tank_gm_ref'] = ''
        self.assertEqual(simulation_grid(s), [4e9, 5e9, 6e9])


if __name__ == '__main__':
    unittest.main()
