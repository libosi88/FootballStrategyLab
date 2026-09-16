import tempfile,unittest,copy
from pathlib import Path
from lab.paper_runner import StreamingPaperRunner
from lab.features import SignalEngine
from lab.common import digest
from lab.contracts import bind_rule
from test_paper_execution import packet
from test_core import event,TEST_CONTRACT

def standard_packet():
    p=packet(['LIVE_OVER']);p['execution_policy'].update(feature_version='v3',cross_stale_minutes=5,warmup='complete_archive_history_required_for_history_based_features')
    p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')});return p

class PaperRunner(unittest.TestCase):
    def test_live_match_end_discards_unsubmitted_and_unready_quotes(self):
        p=standard_packet();e={**event(0,line=8,ts=1),'market':0}
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as runner:
            runner.declare_new_match('a');runner.feed(e)
            self.assertTrue(runner.pending);self.assertEqual(runner.finish_match('a')[0]['status'],'REJECTED')
            self.assertEqual(runner.dispatcher.usage('a'),0);self.assertFalse(runner.pending)
            with self.assertRaises(ValueError):runner.feed({**e,'eid':1,'row':3,'event_key':'file:3','ts':2})
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as runner:
            runner.feed(e);self.assertTrue(runner.waiting)
            runner.finish_match('a');self.assertFalse(runner.waiting)
            with self.assertRaises(ValueError):runner.feed(e)
    def test_rejected_restore_closes_database_connection(self):
        p=standard_packet()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'runner.db'
            with StreamingPaperRunner(path,p,archive_history=True):pass
            with self.assertRaises(ValueError):StreamingPaperRunner(path,p,archive_history=False)
            path.rename(Path(td)/'renamed.db')
    def test_live_start_waits_for_history_and_does_not_backdate_missed_signal(self):
        p=standard_packet();a={**event(0,line=8,ts=1),'market':0};b={**event(1,line=8,ts=2),'market':0}
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            self.assertEqual(r.feed(b)['status'],'WAITING_HISTORY');self.assertEqual(r.dispatcher.usage('a'),0)
            result=r.backfill([a]);self.assertEqual(result['missed_signals'],1);self.assertEqual(r.flush(),[]);self.assertEqual(r.dispatcher.usage('a'),0)
    def test_pending_batch_survives_restart_without_double_fill(self):
        p=standard_packet();a={**event(0,line=8,ts=1),'market':0};b={**event(1,line=8,ts=2),'market':0}
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'runner.db'
            with StreamingPaperRunner(path,p,True,True) as r:self.assertEqual(len(r.feed(a)['signals']),1)
            with StreamingPaperRunner(path,p,True,True) as r:
                result=r.feed(b);self.assertEqual(len(result['intents']),1);self.assertEqual(result['intents'][0]['status'],'FILLED')
                self.assertEqual(r.feed(b)['signals'],[]);self.assertEqual(r.dispatcher.usage('a'),1)
            with StreamingPaperRunner(path,p,True,True) as r:self.assertEqual(r.dispatcher.usage('a'),1);self.assertEqual(r.flush(),[])
    def test_engine_cannot_late_mark_current_quote_as_phase_start(self):
        p=standard_packet();e={**event(0,line=8,ts=1),'market':0};eng=SignalEngine(p['rules'],contract=p['contract'],execution_policy=p['execution_policy'])
        self.assertEqual(eng.feed(e),[])
        with self.assertRaises(ValueError):eng.mark_history_complete('a')
    def test_bad_backfill_does_not_commit_partial_history_readiness(self):
        p=standard_packet();a={**event(0,line=8,ts=1),'market':0};bad={**a,'event_key':'other:2','source':'other','water':[100,85]};current={**event(2,line=8,ts=3),'market':0}
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            r.feed(current)
            with self.assertRaises(ValueError):r.backfill([a,bad])
            self.assertFalse(r.engine.is_history_ready(current));self.assertEqual(len(r.waiting['a']),1);self.assertEqual(r.dispatcher.usage('a'),0)
    def test_pending_v2_checkpoint_can_restore(self):
        import sqlite3,json
        from lab.common import canonical,digest
        p=standard_packet();a={**event(0,line=8,ts=1),'market':0}
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'runner.db'
            with StreamingPaperRunner(path,p,True,True) as r:
                r.feed(a);self.assertTrue(r.pending)
            conn=sqlite3.connect(path);row=conn.execute("SELECT body FROM runner_state WHERE key='coordinator'").fetchone()
            body=json.loads(row[0]);body.pop('sha256');body['payload']['quote_mapping']='minute_close_latest_v2';body['sha256']=digest(body)
            conn.execute('UPDATE runner_state SET body=? WHERE key=?',(canonical(body),'coordinator'));conn.commit();conn.close()
            with StreamingPaperRunner(path,p,True,True) as r:self.assertTrue(r.pending)

    def test_path_evidence_is_per_atom_and_not_mutable_engine_state(self):
        a={'feature':'path2_keep','op':'eq','value':12,'sequence':'subsequence','span':5}
        rule=bind_rule({'id':'r','direction':'LIVE_GIVE','priority':0,'conditions':[a]},TEST_CONTRACT)
        eng=SignalEngine([rule],contract=TEST_CONTRACT);eng.mark_history_complete('a')
        rows=[event(0,line=2,w0=90,ts=0),event(1,line=3,w0=90,ts=1),event(2,line=3,w0=95,ts=2),event(3,line=2,w0=95,ts=3)]
        signals=[]
        for e in rows:signals+=eng.feed(e)
        self.assertEqual(len(signals),1);ev=signals[0]['evidence']['atoms'][0]
        self.assertTrue(ev['value']);self.assertEqual([x['code'] for x in ev['path_events']],[1,2])
        state=eng.snapshot();ev['path_events'].clear();self.assertEqual(eng.snapshot(),state)

if __name__=='__main__':unittest.main()
