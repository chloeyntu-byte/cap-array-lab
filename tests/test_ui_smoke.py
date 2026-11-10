import os
import time
import unittest
from unittest import mock
from caplab.ui import App
from caplab import fieldwork_tk as fw


@unittest.skipUnless(os.environ.get('DISPLAY'),'Tk display required')
class UISmokeTest(unittest.TestCase):
    def setUp(self):
        self.native=mock.patch('caplab.backend.Backend.native',side_effect=AssertionError('Offline UI must not start native tools'))
        self.native.start();self.app=App();self.errors=[]
        self.app.report_callback_exception=lambda *args:self.errors.append(args)
        self.app.geometry('1280x800+0+0');self.pump(.25)

    def tearDown(self):
        self.app.destroy();self.native.stop();self.assertEqual(self.errors,[])

    def pump(self,seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:self.app.update();time.sleep(.01)

    def test_main_window_theme_and_compact_actions(self):
        fw.apply(self.app,product='cap')
        self.assertTrue(self.app.empty_state.winfo_ismapped())
        self.assertEqual(self.app.runbutton.cget('style'),'Primary.TButton')
        for widget in (self.app.runbutton,self.app.stopbutton,self.app.status_chip):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),800)

    def test_example_replay_and_stop(self):
        self.app.load_example();self.pump(.5)
        self.assertEqual(len(self.app.rows),24)
        self.app.runbutton.invoke();self.pump(.35)
        self.assertTrue(self.app.sim_jobs)
        self.assertEqual(self.app._status_state,'running')
        self.app.stopbutton.invoke();self.pump(.3)
        self.assertFalse(self.app.sim_jobs)
        self.assertEqual(self.app._status_state,'error')
        self.app.runbutton.invoke();self.pump(1.6)
        self.assertFalse(self.app.jobs)
        self.assertEqual(self.app._status_state,'done')
        self.assertIn('Synthetic replay complete',self.app.status.get())
        self.assertEqual(self.app.plots.cget('background'),fw.PLOT_BG)
        self.assertEqual(len(self.app.table.get_children()),24)
        self.assertTrue(self.app.table.bbox(self.app.table.get_children()[0]))
