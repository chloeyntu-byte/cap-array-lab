import copy
import os
import unittest
from caplab.model import new_project
from caplab.registers import resolve_connectivity
from caplab.canvas_tools import wire_points, native_wire_plan
from caplab.ui import App


def node(ident,x,y,name):
    return {'id':ident,'kind':'passive','library':'L','view':'symbol','cell':'R','x':x,'y':y,'roles':{'A':name},
            'symbol':{'bbox':[-.2,-.2,.2,.2],'shapes':[{'kind':'rect','points':[-.2,-.2,.2,.2]}],
                      'pins':[{'name':'A','xy':[0,0],'direction':'inputOutput'}]}}


@unittest.skipUnless(os.environ.get('DISPLAY'),'Tk display required')
class CanvasUI(unittest.TestCase):
    def setUp(self):
        self.old_load=App.load_libraries;App.load_libraries=lambda *a,**k:None
        p=new_project();p['instances']=[node('I0',100,100,'ONE'),node('I1',300,100,'TWO'),node('I2',200,30,'THREE'),node('I3',200,170,'FOUR')]
        self.app=App(p);self.errors=[];self.app.report_callback_exception=lambda *args:self.errors.append(args);self.app.update();self.app.canvas.focus_force();self.app.update();self.app.redraw();self.app.update()
    def tearDown(self):
        self.app.destroy();App.load_libraries=self.old_load
        self.assertEqual(self.errors,[])
    def click(self,x,y):
        self.app.canvas.event_generate('<ButtonPress-1>',x=x,y=y);self.app.canvas.event_generate('<ButtonRelease-1>',x=x,y=y);self.app.update()
    def key(self,k):
        self.app.canvas.focus_force();self.app.canvas.event_generate('<KeyPress>',keysym=k);self.app.update()
    def test_wire_junction_rotate_drag_and_alias(self):
        a=self.app
        self.key('w');self.click(100,100);self.click(300,100)
        self.click(200,30);self.click(200,170)
        self.assertEqual(len(a.project['wires']),2)
        def connected():return any('I0.A' in m and 'I2.A' in m for m,labels,attached in resolve_connectivity(a.project))
        self.assertFalse(connected())
        cfg={'nets':{'I0.A':'CAL_NET_A','I1.A':'CAL_NET_A','I2.A':'CAL_NET_B','I3.A':'CAL_NET_B'}}
        plan=native_wire_plan(a.project,cfg)
        self.assertEqual(len(plan),3)
        self.assertTrue(any(p['nets']==['CAL_NET_B'] and abs(p['points'][1][1]-p['points'][0][1]) < 140/190 for p in plan))
        self.key('j');self.click(200,100);self.assertTrue(connected())
        self.key('Escape');self.click(115,115);self.assertEqual(a.selected,'I0');self.key('r')
        self.assertEqual(a.instance()['rotation'],90)
        old=wire_points(a.project,a.project['wires'][0])[0]
        a.canvas.event_generate('<ButtonPress-1>',x=115,y=115);a.canvas.event_generate('<B1-Motion>',x=135,y=125);a.canvas.event_generate('<ButtonRelease-1>',x=135,y=125);a.update()
        new=wire_points(a.project,a.project['wires'][0])[0]
        self.assertEqual((new[0]-old[0],new[1]-old[1]),(20,10))
        self.assertEqual(a.project['instances'][0]['roles']['A'],'ONE')

if __name__=='__main__':unittest.main()
