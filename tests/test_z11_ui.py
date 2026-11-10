"""Exercise the bundled standalone window without invoking any native tools."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from caplab.model import ROOT


@unittest.skipUnless(os.environ.get('DISPLAY') and importlib.util.find_spec('matplotlib'),'Tk and optional Matplotlib required')
class Z11UITest(unittest.TestCase):
    def test_standalone_example_and_compact_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin=Path(folder)/'z11'
            shutil.copytree(str(ROOT/'caplab/z11'),str(plugin),ignore=shutil.ignore_patterns('saved_state','saved_structures','__pycache__'))
            code='''import sys
sys.path.insert(0,sys.argv[1])
from z11_integration import MaestroStudio
app=MaestroStudio()
try:
    app.geometry('1280x800+0+0')
    app.update()
    app._set_initial_splitters()
    app.calculate()
    app.update()
    assert len(app.pages[0]['result']['frequency'])==1001
    assert app.schematic_button.instate(['disabled'])
    assert app.status_chip.winfo_ismapped()
    assert app.status_chip.winfo_rooty()+app.status_chip.winfo_height()<=800
    assert app.plots[0][0].get_facecolor()[0]<.1
finally:app.destroy()
'''
            proc=subprocess.run([sys.executable,'-c',code,str(plugin)],env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
            self.assertEqual(proc.returncode,0,proc.stderr.decode('utf-8',errors='replace'))
