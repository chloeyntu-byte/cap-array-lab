"""Frequency Apply verifies sampled physics and never starts a simulator."""
import copy
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from caplab.backend import Backend, digest
from caplab.model import new_project
from caplab.spectrum import simulation_grid, include_observations, resample


def ideal_spectrum():
    rows = []
    for code, cap in [(0, 1e-12), (1, 2e-12)]:
        for freq in [1e8, 1e9, 1e10]:
            rows.append({'code': code, 'frequency_Hz': freq,
                         'G_S': .001, 'B_S': 2 * math.pi * freq * cap})
    return {'analysis': 'sp', 'rows': rows}


class SpectrumTests(unittest.TestCase):
    def test_grid_bounds_modes_and_observation_validation(self):
        settings = dict(simulation_start='100M', simulation_stop='10G',
                        simulation_points=3, simulation_sweep='log', frequencies='2G')
        grid = simulation_grid(settings)
        self.assertEqual((grid[0], grid[-1]), (1e8, 1e10))
        self.assertAlmostEqual(grid[1] / 1e9, 1., places=13)
        settings['simulation_sweep'] = 'linear'
        self.assertEqual(simulation_grid(settings), [1e8, 5.05e9, 1e10])
        for edits in [dict(simulation_points=1), dict(simulation_stop='0'),
                      dict(simulation_points='2.5'), dict(frequencies='50G'),
                      dict(simulation_sweep='arbitrary')]:
            changed = dict(settings, **edits)
            with self.assertRaises(ValueError):
                simulation_grid(changed)

    def test_exact_observations_replace_roundoff_neighbors(self):
        self.assertEqual(include_observations([1e8, 999999999.9999998, 1e10], [1e9, 2.4e9]),
                         [1e8, 1e9, 2.4e9, 1e10])

    def test_apply_interpolates_admittance_before_deriving_capacitance_and_q(self):
        rows = resample(ideal_spectrum(), '100M 2G 10G')
        self.assertEqual(len(rows), 6)
        for row in rows:
            cap = (row['code'] + 1) * 1e-12
            self.assertAlmostEqual(row['C_F'] / cap, 1., places=13)
            self.assertAlmostEqual(row['Q'], 2 * math.pi * row['frequency_Hz'] * cap / .001, places=11)
        with self.assertRaises(ValueError):
            resample(ideal_spectrum(), '20G')
        duplicate = ideal_spectrum()
        duplicate['rows'].append(duplicate['rows'][0])
        with self.assertRaises(ValueError):
            resample(duplicate, '1G')

    def test_multiregister_scan_identity_is_retained(self):
        spectrum = {'rows': [dict(point=0, COARSE=3, FINE=7, frequency_Hz=f,
                                 G_S=.002, B_S=2 * math.pi * f * 4e-12)
                             for f in (1e8, 1e10)]}
        rows = resample(spectrum, '1G')
        self.assertEqual((rows[0]['point'], rows[0]['COARSE'], rows[0]['FINE']), (0, 3, 7))
        self.assertAlmostEqual(rows[0]['C_F'] / 4e-12, 1.)

    def test_observation_change_reuses_verified_embedded_spectrum_without_native_work(self):
        project = new_project()
        project['settings']['frequencies'] = '1G'
        identity = digest(project)
        spectrum = ideal_spectrum()
        sha = hashlib.sha256(json.dumps(spectrum, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        project['private'] = dict(digest=identity, spectrum=spectrum, spectrum_data_sha256=sha)
        project['settings']['frequencies'] = '2G'
        self.assertEqual(digest(project), identity)
        with patch.object(Backend, 'native', side_effect=AssertionError('Apply started Cadence')):
            self.assertEqual(len(Backend().refresh(project)), 2)
        project['private']['spectrum']['rows'][0]['G_S'] = 12
        with self.assertRaisesRegex(ValueError, 'integrity'):
            Backend().refresh(project)
        project['settings']['simulation_stop'] = '100G'
        self.assertNotEqual(digest(project), identity)

    def test_partial_spectrum_is_rejected_even_when_some_codes_are_present(self):
        cfg = dict(analysis='sp', frequencies=[1e8], simulation_frequencies=[1e8, 1e9], codes=[0, 1])
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'spectrum.tsv'
            file.write_text('TEST\t1\t"nominal"\t"CAP_SP"\tdone\tnil\t"/task/1/CAP_SP"\n'
                            'OUT\t"CODE_VALUE"\t"expr"\t0\n'
                            'SPECTRUM\t100000000\t0.001\t0.1\n', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'frequencies'):
                Backend.read_spectrum(file, cfg)

    def test_live_progress_counts_only_completed_real_points(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            text = 'Spectre completes with 0 errors, 1 warning.\n'
            for point in ('1', '2', 'psf'):
                f = root / 'Interactive.0' / point / 'CAP_SP' / 'psf' / 'spectre.out'
                f.parent.mkdir(parents=True)
                f.write_text(text if point != '2' else 'simulation running\n')
                netlist=f.parent.parent/'netlist'/'input.scs';netlist.parent.mkdir()
                netlist.write_text('parameters CAL_POINT={}'.format(int(point)-1 if point.isdigit() else 0))
            events = []
            backend = Backend(events.append)
            backend._monitor_simulation(dict(root=str(root), total=2, started=0, codes=[0,1], explicit=True, analysis='sp', tests=['CAP_SP']), {})
            progress = [e for e in events if e['kind'] == 'simulation']
            self.assertEqual(progress[-1]['completed'], 1)
            self.assertTrue(any(e.get('level') == 'warning' for e in events))


if __name__ == '__main__':
    unittest.main()
