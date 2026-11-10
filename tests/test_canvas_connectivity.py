import copy
import unittest
from caplab.canvas_tools import node_rotation_transform, native_instance_origin, wire_points
from caplab.registers import resolve_connectivity, assign_net, all_nodes, validate_explicit
from caplab.model import new_project


def node(ident,x,y,pin='A',label='', enabled=True):
    return {'id':ident,'x':x,'y':y,'enabled':enabled,'roles':{pin:label},
            'symbol':{'bbox':[-1,-1,1,1],'pins':[{'name':pin,'xy':[0,0]}],'shapes':[]}}


def wire(a,b,points=None,ident='W1'):
    return {'id':ident,'a':a,'b':b,'points':points or []}


def grouped(p,a,b):
    return any(a in members and b in members for members,labels,attached in resolve_connectivity(p))


class CanvasConnectivity(unittest.TestCase):
    def test_named_alias_and_move(self):
        p={'version':3,'instances':[node('I0',0,0,label='ONE'),node('I1',200,0,label='TWO'),node('I2',400,0,label='TWO')], 'registers':[], 'wires':[wire({'pin':'I0.A'},{'pin':'I1.A'})]}
        self.assertTrue(grouped(p,'I0.A','I2.A'))
        old=wire_points(p,p['wires'][0]);p['instances'][0]['x']=40
        self.assertEqual(wire_points(p,p['wires'][0])[0][0],old[0][0]+40)
        assign_net(p,'I0.A','RENAMED');self.assertEqual(len(p['wires']),1)
        self.assertTrue(grouped(p,'I0.A','I2.A'))

    def test_crossing_and_junction(self):
        p={'instances':[node('I0',-20,0),node('I1',20,0),node('I2',0,-20),node('I3',0,20)],'wires':[wire({'pin':'I0.A'},{'pin':'I1.A'}),wire({'pin':'I2.A'},{'pin':'I3.A'},ident='W2')]}
        self.assertFalse(grouped(p,'I0.A','I2.A'))
        p['junctions']=[[0,0]];self.assertTrue(grouped(p,'I0.A','I2.A'))

    def test_bus_width(self):
        p={'instances':[node('I0',0,0,'A<1:0>'),node('I1',50,0,'B')], 'wires':[wire({'pin':'I0.A<1:0>'},{'pin':'I1.B'})]}
        with self.assertRaisesRegex(ValueError,'Bus width mismatch'):resolve_connectivity(p)

    def test_validation_physical_fixture_short(self):
        p=new_project();p['instances']=[node('I0',0,0,label='P'),node('I1',100,0,label='N')]
        p['wires']=[wire({'pin':'I0.A'},{'pin':'I1.A'})]
        with self.assertRaisesRegex(ValueError,'physically shorted'):validate_explicit(p)

    def test_disabled_node_does_not_participate(self):
        p=new_project();p['instances']=[node('I0',0,0,label='P'),node('I1',100,0,label='N'),node('I2',200,0,label='P',enabled=False)]
        cfg=validate_explicit(p)
        self.assertNotIn('I2.A',cfg['nets'])

    def test_disabled_and_rotation(self):
        a=node('I0',10,20);a['symbol']['pins'][0]['xy']=[1,0];a['rotation']=90
        p={'instances':[a,node('I1',0,0,enabled=False)]}
        self.assertEqual(len(all_nodes(p)),1);self.assertEqual(len(all_nodes(p,True)),2)
        self.assertEqual(node_rotation_transform(a,1,0),(10, -170))
        x,y=native_instance_origin(a);self.assertAlmostEqual(x,10/190);self.assertAlmostEqual(y,-20/190)
        before=copy.deepcopy(p);resolve_connectivity(p);self.assertEqual(before,p)

if __name__=='__main__':unittest.main()
