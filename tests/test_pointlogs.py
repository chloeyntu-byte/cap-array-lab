import tempfile
import unittest
from pathlib import Path
from caplab.pointlogs import point_for_row, _read_update

class PointLogs(unittest.TestCase):
 def test_exact_zero_based_snapshot(self):
  points={'0':{'point':0,'path':'old0'},'1':{'point':1,'path':'old1'}}
  self.assertEqual(point_for_row(points,{'point':1})['path'],'old1')
  self.assertIsNone(point_for_row(points,{'code':1}));self.assertIsNone(point_for_row(points,{'point':2}))
 def test_incremental_read_and_rotation(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'spectre.out';self.assertEqual(_read_update(d,0,None)[0],None)
   path.write_text('first\n');text,off,sig,state=_read_update(d,0,None);self.assertEqual((text,state),('first\n','reset'))
   with path.open('a') as out:out.write('second\n')
   text,off,sig,state=_read_update(d,off,sig);self.assertEqual((text,state),('second\n','append'))
   path.write_text('x');text,off,sig,state=_read_update(d,off,sig);self.assertEqual((text,state),('x','reset'))

if __name__=='__main__':unittest.main()
