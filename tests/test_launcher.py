import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from caplab.model import ROOT


class LauncherTest(unittest.TestCase):
    def test_user_native_executable_and_optional_site_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fake=root/'tool';output=root/'args.json';site=root/'site.sh'
            fake.write_text('#!/usr/bin/python3\nimport os,sys,json\nopen(os.environ["CAPLAB_TEST_OUTPUT"],"w").write(json.dumps([sys.argv[1:],os.environ.get("CAPLAB_TEST_SITE")]))\n',encoding='utf-8');fake.chmod(0o700)
            site.write_text('export CAPLAB_TEST_SITE=provided\n',encoding='utf-8')
            env=dict(os.environ,CAPLAB_NATIVE_VIRTUOSO=str(fake),CAPLAB_CADENCE_ENV=str(site),CAPLAB_TEST_OUTPUT=str(output))
            env.pop('CAPLAB_NATIVE_ENV_JSON',None);env.pop('CAPLAB_NATIVE_CDS_LIB',None)
            subprocess.check_call(['bash',str(ROOT/'cadence-child.sh'),'fixture.il','fixture-output','gui'],env=env)
            args,marker=json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(args,['-nocdsinit','-restore','fixture.il','-log','fixture-output']);self.assertEqual(marker,'provided')
            subprocess.check_call(['bash',str(ROOT/'cadence-child.sh'),'fixture.il','fixture-output'],env=env)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))[0][0],'-nograph')

    def test_explicit_python_is_not_silently_replaced(self):
        result=subprocess.run(['bash',str(ROOT/'launch.sh'),'--help'],env=dict(os.environ,CAPLAB_PYTHON='/nonexistent/python'),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode,0)
