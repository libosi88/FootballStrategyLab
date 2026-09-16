"""Independent, deliberately slow prefix reference for existing feature semantics."""
import unittest, random
from lab.common import MISSING
from lab.features import FeatureStream
from test_core import event


def prefix_reference(history,side):
    e=history[-1]
    if not e['valid']:return None
    valid=[x for x in history if x['valid']];cur=e['water'][side];L=e['line']
    prior=valid[:-1] or [e]
    first=valid[0];same=next(x for x in valid if x['line']==L)
    f={'line_init':L-first['line'],'samewater':cur-same['water'][side],
       'water_init':cur-first['water'][side],
       'line_from_min':L-min(x['line'] for x in prior),
       'line_from_max':L-max(x['line'] for x in prior),
       'water_from_min':cur-min(x['water'][side] for x in prior),
       'water_from_max':cur-max(x['water'][side] for x in prior),
       'water_from_current_min':cur-min(x['water'][side] for x in valid)}
    last_invalid=max((i for i,x in enumerate(history[:-1]) if not x['valid']),default=-1)
    for mode,seq in [('keep',valid),('reset',[x for x in history[last_invalid+1:] if x['valid']])]:
        p=seq[-2] if len(seq)>1 else None
        dp=L-p['line'] if p else MISSING
        dw=cur-p['water'][side] if p else MISSING
        f['line_prev_'+mode]=dp;f['water_prev_'+mode]=dw if dp==0 else MISSING
        start=len(seq)-1
        while start>0 and seq[start-1]['line']==L:start-=1
        f['returnwater_'+mode]=cur-seq[start]['water'][side]
        steps=[];line_steps=[]
        for a,b in zip(seq[:-1],seq[1:]):
            d=b['line']-a['line'];w=b['water'][side]-a['water'][side]
            code=(1 if d>0 else 2 if d<0 else 3 if w>0 else 4 if w<0 else 0)
            if code:steps.append((code,b['ts']))
            if d:line_steps.append(1 if d>0 else 2)
        f['pulse_'+mode]=int(bool(p) and (dp!=0 or dw!=0))
        for k in (1,2,3):
            f[f'path{k}_{mode}']=int(''.join(str(x[0]) for x in steps[-k:])) if len(steps)>=k else MISSING
            f[f'span{k}_{mode}']=steps[-1][1]-steps[-k][1] if len(steps)>=k else MISSING
        f['linepath2_'+mode]=10*line_steps[-2]+line_steps[-1] if len(line_steps)>=2 else MISSING
    return f

class FeatureReference(unittest.TestCase):
    def test_prefix_recomputation(self):
        rng=random.Random(20260912);history=[];stream=FeatureStream();L=0;w0=95;w1=85
        for i in range(150):
            L+=rng.choice([-1,0,0,1]);w0=max(20,w0+rng.choice([-10,0,0,5]));w1=max(20,w1+rng.choice([-5,0,0,10]))
            e=event(i,L,w0,w1,valid=(i%19!=8),ts=i//3)
            history.append(e);got=stream.feed(e)
            if not e['valid']:self.assertIsNone(got);continue
            for side in (0,1):
                ref=prefix_reference(history,side)
                self.assertEqual({k:got[side][k] for k in ref},ref)
    def test_future_extreme_does_not_change_prefix(self):
        hh=[event(0,w0=95),event(1,w0=85),event(2,w0=90),event(3,w0=1)]
        self.assertEqual(prefix_reference(hh[:3],0)['water_from_current_min'],5)
        s=FeatureStream();out=[s.feed(e) for e in hh]
        self.assertEqual(out[2][0]['water_from_current_min'],5)
    def test_timed_three_step_last_endpoint(self):
        hh=[event(0,ts=1),event(1,w0=90,ts=2),event(2,w0=100,ts=17),event(3,w0=95,ts=32),event(4,w0=95,ts=50)]
        s=FeatureStream();got=None
        for e in hh:got=s.feed(e)
        ref=prefix_reference(hh,0)
        self.assertEqual(got[0]['span3_keep'],30);self.assertEqual(ref['span3_keep'],30)
        self.assertEqual(got[0]['pulse_keep'],0)

if __name__=='__main__':unittest.main()
