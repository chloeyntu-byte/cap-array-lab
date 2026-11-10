"""The bundled nodal solver is checked against a separate closed-form network."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'caplab/z11'))
import numpy as np
from network_model import example_document
from network_model_v2 import solve
from z11_model import z11
from optimizer import TraceScore
from z11_schematic import build_plan, serialize_skill


class Z11NumericTest(unittest.TestCase):
    def test_nodal_solution_matches_closed_form(self):
        doc=example_document();frequency=np.linspace(.1,30,101)
        values={p['id']:p['value'] for p in doc['parts'] if p['kind']!='P'}
        quality={p['id']:p['q'] for p in doc['parts'] if p['kind']!='P'}
        actual,ignored=solve(doc,frequency)
        np.testing.assert_allclose(actual,z11(frequency,values,quality),rtol=1e-10,atol=1e-8)
        self.assertEqual(ignored,0)

    def test_native_export_plan_is_data_only(self):
        plan=build_plan(example_document(),reference_frequency_ghz=1)
        text=serialize_skill(['generic_test',plan])
        self.assertIn('generic_test',text)
        self.assertIn('analogLib',text)

    def test_trace_score_defaults(self):
        score=TraceScore(False)
        self.assertFalse(score.feasible)
        self.assertIsNone(score.value)
        self.assertEqual(score.violation,float('inf'))
