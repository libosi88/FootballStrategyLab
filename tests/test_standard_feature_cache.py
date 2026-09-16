import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from lab.common import DEFAULT,DIRECTIONS,MISSING,read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.standard_feature_cache import prepare_standard_features
from lab.standard_mining import ordered_standard_events
from lab.mining import build_direction,Paused,ResourcePaused
from test_core import event


class StandardFeatureCache(unittest.TestCase):
    def fixture(self):
        events=[];labels={}
        for mid in range(3):
            sid=f'cache-{mid}';labels[sid]={'eligible':mid!=1,'year':2024+mid,'final':[2,1]}
            for phase in (0,1):
                for step in range(6):
                    for market in (0,1):
                        e=event(len(events),line=(0,1,-1,2,0,-2)[step] if market else 8+step%3,w0=90+step,w1=100-step,ts=phase*20+step,valid=step!=3)
                        e.update(sid=sid,mid=mid,phase=phase,market=market,status='滚' if phase else '早')
                        events.append(e)
        return ordered_standard_events(events),labels
    def test_all_sixteen_caches_equal_individual_causal_streams(self):
        events,labels=self.fixture()
        with tempfile.TemporaryDirectory() as td:
            result=prepare_standard_features(td,events,labels,100,DEFAULT,lambda **kw:None,lambda:False)
            self.assertEqual(result['event_passes'],1)
            for direction in DIRECTIONS:
                expected=build_direction(events,labels,100,direction,DEFAULT)
                with np.load(Path(td)/'mining'/direction/'arrays.npz',allow_pickle=False) as actual:
                    for key in expected:np.testing.assert_array_equal(expected[key],actual[key],err_msg=direction+'/'+key)
                    for key in set(actual)-set(expected):self.assertTrue(np.all(actual[key]==MISSING))
            self.assertEqual(prepare_standard_features(td,events,labels,100,DEFAULT,lambda **kw:None,lambda:False)['status'],'REUSED')
            path=Path(td)/'mining/PRE_GIVE/arrays.npz';path.write_bytes(path.read_bytes()+b'tamper')
            with self.assertRaisesRegex(ValueError,'完整性'):prepare_standard_features(td,events,labels,100,DEFAULT,lambda **kw:None,lambda:False)
    def test_pause_preserves_only_completed_caches_and_closes_windows_maps(self):
        events,labels=self.fixture();calls=0
        def pause():
            nonlocal calls
            calls+=1;return calls==5
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(Paused):prepare_standard_features(td,events,labels,100,DEFAULT,lambda **kw:None,pause)
            first=read_json(Path(td)/'mining/PRE_GIVE/standard_features.json')
            self.assertIsNotNone(first);self.assertFalse(list(Path(td).glob('feature_preparation_*')))
            resumed=prepare_standard_features(td,events,labels,100,DEFAULT,lambda **kw:None,lambda:False)
            self.assertLess(resumed['directions_built'],16)
            self.assertEqual(read_json(Path(td)/'mining/PRE_GIVE/standard_features.json'),first)
    def test_disk_limit_does_not_shrink_requested_directions(self):
        events,labels=self.fixture()
        with tempfile.TemporaryDirectory() as td,patch('lab.standard_feature_cache.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaises(ResourcePaused):prepare_standard_features(td,events,labels,100,DEFAULT,lambda **kw:None,lambda:False)
            self.assertFalse(list(Path(td).glob('mining/*/standard_features.json')))


if __name__=='__main__':unittest.main()
