import itertools,random,unittest
import numpy as np
from lab.common import MISSING
from lab.standard_spec import scalar_atoms,minute_atoms,time_windows,path_atoms,SPANS
from lab.standard_features import StandardFeatureStream,path_events,path_matches,predicate_key,minute_parts
from lab.standard_kernels import path_predicate,bound_window_columns
from test_core import event,TEST_CONTRACT

def brute_path(rows,side,a,key):
    steps=path_events(rows,side,a.get('event_model')=='composite')
    pattern=a.get('pattern',[int(v) for v in str(a['value'])]);k=len(pattern)
    if len(steps)<k:return False
    choices=[tuple(range(len(steps)-k,len(steps)))] if a.get('sequence','contiguous')=='contiguous' else itertools.combinations(range(len(steps)),k)
    for chosen in choices:
        if chosen[-1]!=len(steps)-1:continue
        ss=[steps[i] for i in chosen]
        if a.get('pulse') and ss[-1][4]!=key:continue
        if 'span' in a and ss[-1][1]-ss[0][1]>a['span']:continue
        good=True
        for step,code in zip(ss,pattern):
            attrs={code} if code<10 else {code//10,code%10}
            good &= step[0]==code and (not attrs&{1,2} or step[2]>=a.get('min_line_step',1)) and (not attrs&{3,4} or step[3]>=a.get('min_water_step',1))
        if good:return True
    return False

class StandardGrammar(unittest.TestCase):
    def test_full_scalar_grids_and_widths(self):
        w=list(scalar_atoms('water',93,107,100,'W','water'));c=list(scalar_atoms('water',93,107,100,'C','water'))
        self.assertIn({'feature':'water','op':'eq','value':94},w)
        self.assertIn({'feature':'water','op':'ge','value':110},c)
        self.assertIn({'feature':'water','op':'range','value':95,'upper':110},c)
        self.assertEqual({a['upper']-a['value'] for a in w if a['op']=='range'},{5,10,15,20,30,50})
        self.assertEqual({a['upper']-a['value'] for a in scalar_atoms('line',-3,4,4,'W','line') if a['op']=='range'},{1,2,3,4,6,8})
    def test_time_windows_and_fine_minutes(self):
        self.assertIn((1,3),time_windows('W'));self.assertIn((5,50),time_windows('C'))
        self.assertTrue(set(time_windows('C'))<=set(time_windows('W')))
        self.assertIn({'feature':'minute','op':'eq','value':47},list(minute_atoms('W')))
        self.assertNotIn({'feature':'minute','op':'eq','value':47},list(minute_atoms('C')))
    def test_full_t_cartesian_parameters(self):
        atoms=list(path_atoms(100,True));selected=[a for a in atoms if a['feature']=='path2_reset' and a['value']==13]
        self.assertEqual(len(selected),len(SPANS)*4*5*2*2)
        self.assertEqual({a['min_line_step'] for a in selected},{1,2,3,4})
        self.assertEqual({a['min_water_step'] for a in selected},{1,5,10,15,20})
        self.assertEqual({a.get('span') for a in selected},set(SPANS))

class StandardCausality(unittest.TestCase):
    def test_inserted_step_and_state_endpoint(self):
        a={'feature':'path2_keep','op':'eq','value':12,'sequence':'subsequence','span':5}
        stream=StandardFeatureStream(contract=TEST_CONTRACT,atoms=[a]);out=[]
        for e in [event(0,line=2,w0=90,ts=0),event(1,line=3,w0=90,ts=1),event(2,line=3,w0=95,ts=2),event(3,line=2,w0=95,ts=3),event(4,line=2,w0=95,ts=100),event(5,line=2,w0=100,ts=101)]:out.append(stream.feed(e)[0][predicate_key(a)])
        self.assertEqual(out,[False,False,False,True,True,False])
    def test_composite_does_not_invent_two_quotes(self):
        history=[event(0,line=2,w0=90),event(1,line=3,w0=95)]
        one={'feature':'path1_keep','op':'eq','value':0,'pattern':[13],'event_model':'composite'}
        two={**one,'feature':'path2_keep','pattern':[1,3],'sequence':'subsequence'}
        self.assertTrue(path_matches(history,0,one,history[-1]['event_key']))
        self.assertFalse(path_matches(history,0,two,history[-1]['event_key']))
    def test_cross_uses_previous_complete_minute_and_closed_state(self):
        stream=StandardFeatureStream(contract=TEST_CONTRACT,cross_stale_minutes=5)
        stream.feed({**event(0,line=10,ts=0,valid=False),'market':0})
        stream.feed(event(1,line=2,ts=0))
        stream.feed({**event(2,line=12,ts=1),'market':0})
        self.assertEqual(stream.feed(event(3,line=2,ts=1))[0]['cross_line'],MISSING)
        f=stream.feed(event(4,line=2,ts=2))[0];self.assertEqual(f['cross_line'],12);self.assertEqual(f['cross_line_init'],0)
        stream.feed({**event(5,line=12,ts=2,valid=False),'market':0})
        self.assertEqual(stream.feed(event(6,line=2,ts=3))[0]['cross_line'],MISSING)
    def test_window_first_precedes_other_conditions(self):
        a={'feature':'window_10_15_water_init','op':'le','value':-5}
        stream=StandardFeatureStream(contract=TEST_CONTRACT,atoms=[a])
        stream.feed({**event(0,w0=100,valid=False),'minute':10})
        self.assertEqual(stream.feed({**event(1,w0=95),'minute':11})[0][a['feature']],0)
        self.assertEqual(stream.feed({**event(2,w0=90),'minute':12})[0][a['feature']],-5)
        self.assertEqual(stream.feed({**event(3,w0=90),'minute':15})[0][a['feature']],MISSING)
    def test_dynamic_role_history_is_distinct_from_actual_team_history(self):
        stream=StandardFeatureStream(contract=TEST_CONTRACT)
        stream.feed(event(0,line=1,w0=90,w1=80));f=stream.feed(event(1,line=-1,w0=100,w1=85))[1]
        self.assertEqual(f['water_init'],5);self.assertEqual(f['role_water_init'],-5)
    def test_pregame_summary_freezes_at_observed_live_boundary(self):
        stream=StandardFeatureStream(contract=TEST_CONTRACT)
        stream.feed({**event(0,line=2,ts=0),'phase':0,'status':'早'})
        stream.feed({**event(1,line=3,ts=1),'phase':0,'status':'即'})
        f=stream.feed(event(2,line=4,ts=2))[0];self.assertEqual(f['pre_line_change'],1);self.assertEqual(f['pre_line_close'],3)
        stream.feed({**event(3,line=9,ts=3),'phase':0,'status':'即'})
        self.assertEqual(stream.feed(event(4,line=5,ts=4))[0]['pre_line_close'],3)
    def test_explicit_stoppage_preserves_identity(self):
        self.assertEqual(minute_parts('45+2'),(45,2,47));self.assertEqual(minute_parts('中场'),(MISSING,)*3)

class IndependentKernels(unittest.TestCase):
    def test_paths_against_exhaustive_subsequence_reference(self):
        rng=random.Random(319);rows=[];line=2;water=90
        for i in range(24):
            line+=rng.choice((-2,-1,0,1,2));water+=rng.choice((-5,0,5));rows.append(event(i,line=line,w0=water,ts=i//2,valid=i not in (9,16)))
        mids=np.zeros(len(rows),np.int64);valid=np.array([e['valid'] for e in rows]);lines=np.array([e['line'] for e in rows]);waters=np.array([e['water'][0] for e in rows]);times=np.array([e['ts'] for e in rows])
        for composite,reset,pulse,subseq in itertools.product((False,True),repeat=4):
            for pattern in ((1,2),(1,3,2),(13,2) if composite else (3,4)):
                a={'feature':f'path{len(pattern)}_'+('reset' if reset else 'keep'),'op':'eq','value':int(''.join(map(str,pattern))) if not composite else 0,'sequence':'subsequence' if subseq else 'contiguous','pulse':pulse,'span':5,'min_line_step':1,'min_water_step':5}
                if composite:a.update(event_model='composite',pattern=list(pattern))
                got=path_predicate(mids,valid,lines,waters,times,np.array(pattern,np.int64),reset,pulse,subseq,composite,1,5,5)
                history=[];expected=[]
                for e in rows:
                    if not e['valid']:
                        if reset:history=[]
                        expected.append(False);continue
                    history.append(e);expected.append(brute_path(history,0,a,e['event_key']))
                self.assertEqual(got.tolist(),expected,(composite,reset,pulse,subseq,pattern))
                stream=StandardFeatureStream(contract=TEST_CONTRACT,atoms=[a,a])
                streamed=[]
                for e in rows:
                    result=stream.feed(e);streamed.append(False if result is None else result[0][predicate_key(a)])
                self.assertEqual(streamed,expected,('stream',composite,reset,pulse,subseq,pattern))
    def test_window_kernel(self):
        mids=np.array([0,0,0,1,1]);valid=np.array([False,True,True,True,True]);minute=np.array([10,11,15,12,13]);line=np.arange(5);w0=np.arange(90,95);w1=np.arange(80,85)
        out=bound_window_columns(mids,valid,minute,line,w0,w1,10,15)
        self.assertEqual(out[:,0].tolist(),[MISSING,0,MISSING,0,1])

if __name__=='__main__':unittest.main()
