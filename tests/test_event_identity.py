import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from lab.common import digest
from lab.contracts import bind_rule,STATE_SCHEMA
from lab.features import FeatureStream,SignalEngine,identity_scope
from lab.standard_features import StandardFeatureStream
from lab.paper_runner import StreamingPaperRunner
from test_core import event,TEST_CONTRACT
from test_paper_runner import standard_packet

def packet():
    p=standard_packet()
    p['rules']=[bind_rule({'id':'samewater_probe','priority':0,'direction':'LIVE_GIVE','conditions':[{'feature':'samewater','op':'le','value':-10}]},TEST_CONTRACT)]
    p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')})
    return p

class EventIdentity(unittest.TestCase):
    def sequence(self,closed=True):
        a=event(0,ts=10,w0=95);b=event(1,ts=11,valid=not closed)
        return a,b,{**a,'ts':12,'water':[75,85]}

    def test_nonadjacent_reuse_rejected_before_any_feature_mutation(self):
        for cls in (FeatureStream,StandardFeatureStream):
            for closed in (True,False):
                stream=cls(contract=TEST_CONTRACT);a,b,bad=self.sequence(closed)
                stream.feed(a);stream.feed(b);before=copy.deepcopy(stream.state)
                self.assertIsNone(stream.feed(copy.deepcopy(a)));self.assertEqual(before,stream.state)
                for changed in (bad,{**a,'ts':12},{**a,'water':[75,85]}):
                    with self.assertRaisesRegex(ValueError,'同一事件ID'):stream.feed(changed)
                    self.assertEqual(before,stream.state)

    def test_no_false_signal_or_fired_entry_before_or_after_restart(self):
        p=packet();a,b,bad=self.sequence();engine=SignalEngine(p['rules'],contract=p['contract'],execution_policy=p['execution_policy']);engine.mark_history_complete('a')
        self.assertEqual(engine.feed(a),[]);self.assertEqual(engine.feed(b),[])
        engine=SignalEngine(p['rules'],engine.snapshot(),contract=p['contract'],execution_policy=p['execution_policy']);before=engine.snapshot()
        with self.assertRaises(ValueError):engine.feed(bad)
        self.assertEqual(engine.snapshot(),before);self.assertFalse(engine.fired)
        clean=event(2,ts=12,w0=100);self.assertEqual(engine.feed(clean),[])

    def test_closed_quote_identity_and_cross_market_changes_are_protected(self):
        stream=StandardFeatureStream(contract=TEST_CONTRACT);a,b,_=self.sequence()
        stream.feed(a);stream.feed(b);stream.feed(event(2,ts=12,w0=100));before=copy.deepcopy(stream.state)
        for bad in ({**b,'ts':13},{**a,'ts':13,'market':0},{**a,'ts':13,'phase':0,'status':'早'}):
            with self.assertRaises(ValueError):stream.feed(bad)
            self.assertEqual(stream.state,before)

    def test_capacity_fails_closed_without_evicting_old_identity(self):
        with patch('lab.features.MAX_EVENT_KEYS_PER_MATCH',2):
            stream=StandardFeatureStream(contract=TEST_CONTRACT);a,b,_=self.sequence()
            stream.feed(a);stream.feed(b);before=copy.deepcopy(stream.state)
            with self.assertRaisesRegex(ValueError,'容量'):stream.feed(event(2,ts=12))
            self.assertEqual(stream.state,before);self.assertIsNone(stream.feed(a))
            with self.assertRaisesRegex(ValueError,'同一事件ID'):stream.feed({**a,'ts':12})

    def test_match_close_releases_registry_but_tombstone_blocks_replay(self):
        p=packet();engine=SignalEngine(p['rules'],contract=p['contract'],execution_policy=p['execution_policy']);engine.mark_history_complete('a')
        a=event(0);engine.feed(a);self.assertIn(identity_scope(a),engine.stream.state['_event_identities'])
        engine.close_match('a');self.assertNotIn(identity_scope(a),engine.stream.state['_event_identities'])
        with self.assertRaises(ValueError):engine.feed(a)

    def test_old_or_incomplete_identity_snapshot_is_not_silently_upgraded(self):
        p=packet();engine=SignalEngine(p['rules'],contract=p['contract'],execution_policy=p['execution_policy']);engine.mark_history_complete('a');engine.feed(event(0))
        state=engine.snapshot();state['schema']='FSL_signal_state_v4';state['payload_hash']=digest({k:v for k,v in state.items() if k!='payload_hash'})
        with self.assertRaises(ValueError):SignalEngine(p['rules'],state,contract=p['contract'],execution_policy=p['execution_policy'])
        raw=copy.deepcopy(engine.stream.state);raw['_event_identities']={}
        with self.assertRaises(ValueError):FeatureStream(raw,TEST_CONTRACT)
        self.assertEqual(STATE_SCHEMA,'FSL_signal_state_v5')

    def test_paper_replay_cannot_rewrite_quote_or_create_order(self):
        p=packet();a,b,bad=self.sequence()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ledger.db'
            with StreamingPaperRunner(path,p,archive_history=True,auto_fill=True) as runner:
                runner.feed(a);runner.feed(b);before=runner.engine.snapshot()
                with self.assertRaises(ValueError):runner.feed(bad)
                self.assertEqual(before,runner.engine.snapshot());self.assertEqual(runner.dispatcher.usage('a'),0)
                self.assertEqual(runner.feed(a)['status'],'DUPLICATE_IGNORED')
                self.assertFalse(runner.pending)
            with StreamingPaperRunner(path,p,archive_history=True,auto_fill=True) as runner:
                with self.assertRaises(ValueError):runner.feed(bad)
                self.assertEqual(runner.dispatcher.usage('a'),0)

    def test_same_minute_duplicate_cannot_replace_latest_pending_quote(self):
        p=standard_packet();a={**event(0,line=8,ts=10),'market':0};b={**event(1,line=8,w0=75,ts=10),'market':0}
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'ledger.db',p,archive_history=True) as runner:
            runner.feed(a);runner.feed(b);before=copy.deepcopy(runner.pending)
            self.assertEqual(runner.feed(a)['status'],'DUPLICATE_IGNORED');self.assertEqual(runner.pending,before)

    def test_backfill_cannot_discard_conflicting_waiting_quote_as_duplicate(self):
        p=packet();a=event(0,ts=10)
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'ledger.db',p) as runner:
            runner.feed(a);before=(runner.engine.snapshot(),copy.deepcopy(runner.waiting))
            with self.assertRaisesRegex(ValueError,'同一事件ID'):runner.backfill([{**a,'water':[75,85]}])
            self.assertEqual((runner.engine.snapshot(),runner.waiting),before)
