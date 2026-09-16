"""Coordinator fixes for the standard JSON paper stream: match_start, history backfill and late quotes."""
import json,tempfile,unittest
from pathlib import Path
from lab.common import digest
from lab.contracts import bind_rule
from lab.paper_runner import StreamingPaperRunner
from lab.standard_cli import paper_stream
from test_core import event,TEST_CONTRACT

def pk(conditions,direction='LIVE_OVER'):
    rules=[bind_rule({'id':'r0','direction':direction,'conditions':conditions,'priority':0},TEST_CONTRACT)]
    policy={'match_cap':4,'stale_minutes':5,'priority':'frozen_rule_priority','live_enabled':False,'second_slot_enabled':False,'feature_version':'v3','cross_stale_minutes':5}
    p={'rules':rules,'contract':TEST_CONTRACT,'execution_policy':policy}
    p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')});return p

def ou(i,ts,w0,w1=85,market=0):
    return {**event(i,line=8,ts=ts,w0=w0,w1=w1),'market':market}

def alarms(runner):
    return [row['kind'] for row in runner.dispatcher.conn.execute('SELECT kind FROM alarms ORDER BY id')]

def executions(runner):
    """(status, first-signal minute, execution minute) of every reserved paper order."""
    out=[]
    for row in runner.dispatcher.conn.execute('SELECT status,body FROM intents ORDER BY rowid'):
        body=json.loads(row['body']);out.append((row['status'],body['signal']['ts'],body['execution_ts']))
    return out

class MatchStartDeclaration(unittest.TestCase):
    def test_match_start_rejected_while_quotes_wait_for_history(self):
        # water_init<=0 AND water>=90 never fires on the true history; only a fabricated phase start would trigger it.
        p=pk([{'feature':'water_init','op':'le','value':0},{'feature':'water','op':'ge','value':90}])
        a,b,c=ou(0,1,80),ou(1,2,90),ou(2,3,85)
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            self.assertEqual(r.feed(a)['status'],'WAITING_HISTORY')
            with self.assertRaisesRegex(ValueError,'等待历史'):r.declare_new_match('a')
            self.assertEqual(len(r.waiting['a']),1);self.assertFalse(r.engine.is_history_ready(a))
            r.backfill([a])
            self.assertEqual(r.feed(b)['signals'],[]);self.assertEqual(r.feed(c)['signals'],[])
            self.assertEqual(r.dispatcher.usage('a'),0)
    def test_match_start_still_accepted_without_waiting_quotes(self):
        p=pk([{'feature':'water','op':'ge','value':100}])
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            r.declare_new_match('a')
            self.assertEqual(r.feed(ou(0,1,95))['status'],'PAPER_ONLY')

class HistoryBackfill(unittest.TestCase):
    def test_resent_history_keeps_a_ready_match_pending_batch(self):
        p=pk([{'feature':'water','op':'ge','value':100}])
        a,b=ou(0,1,95),ou(1,2,100)
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            r.declare_new_match('a');r.feed(a)
            self.assertEqual(len(r.feed(b)['signals']),1);self.assertTrue(r.pending)
            self.assertEqual(r.backfill([a])['missed_signals'],0)
            # The match never had a history gap, so its unsubmitted batch must survive the duplicate history.
            self.assertTrue(r.pending);self.assertNotIn('missed_submission_during_history_gap',alarms(r))
            self.assertEqual([x['status'] for x in r.feed(ou(2,3,100))['intents']],['RESERVED'])
            self.assertEqual(r.dispatcher.usage('a'),1)
    def test_backfilled_queue_is_submitted_once_at_the_latest_observed_minute(self):
        p=pk([{'feature':'water','op':'ge','value':100}])
        live=[ou(1,3,95),ou(2,4,100),ou(3,5,101)]
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            for e in live:self.assertEqual(r.feed(e)['status'],'WAITING_HISTORY')
            self.assertEqual(r.backfill([ou(0,1,95)])['replayed_queued_quotes'],3)
            # One order, decided at the latest observed minute, never backdated to the buffered minute.
            self.assertEqual(executions(r),[('RESERVED',4,5)])
            self.assertEqual(r.watermarks['a'],5);self.assertEqual(r.dispatcher.usage('a'),1)
    def test_backfill_rejects_a_stale_first_signal_instead_of_filling_at_a_superseded_price(self):
        p=pk([{'feature':'water','op':'ge','value':100}])
        live=[ou(1,3,95),ou(2,4,100),ou(3,5,101),ou(4,100,90)]
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            for e in live:r.feed(e)
            r.backfill([ou(0,1,95)])
            self.assertEqual(executions(r),[]);self.assertIn('missed_stale_unsubmitted_batch',alarms(r))
            self.assertFalse(r.pending);self.assertEqual(r.watermarks['a'],100)
            self.assertEqual(r.dispatcher.usage('a'),0)

class LateQuotes(unittest.TestCase):
    def test_late_cross_market_quote_after_backfill_is_rejected_without_faulting(self):
        p=pk([{'feature':'water','op':'ge','value':200}])
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            r.backfill([ou(0,5,95,market=0),ou(1,10,95,market=1)])
            self.assertEqual(r.watermarks['a'],10)
            with self.assertRaises(ValueError):r.feed(ou(2,7,95,market=0))
            # A late message is business input to reject; the coordinator stays usable.
            self.assertFalse(r._faulted)
            self.assertEqual(r.feed(ou(3,11,95,market=1))['status'],'PAPER_ONLY')
    def test_quote_older_than_processed_clock_is_rejected_when_the_fence_lags(self):
        p=pk([{'feature':'water','op':'ge','value':200}])
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'runner.db',p) as r:
            r.declare_new_match('a');r.feed(ou(0,5,95));r.feed(ou(1,10,95))
            # A checkpoint that lost its time fence must not let a late quote reach the engine and fault it.
            r.watermarks.pop('a',None)
            with self.assertRaisesRegex(ValueError,'迟到'):r.feed(ou(2,7,95))
            self.assertFalse(r._faulted)
            self.assertEqual(r.feed(ou(3,11,95))['status'],'PAPER_ONLY')

class PaperStreamMessages(unittest.TestCase):
    def run_stream(self,packet,lines,folder):
        rules=Path(folder)/'rules.json';rules.write_text(json.dumps(packet,ensure_ascii=False),encoding='utf-8')
        source=Path(folder)/'in.jsonl';output=Path(folder)/'out.jsonl'
        source.write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in lines)+'\n',encoding='utf-8')
        result=paper_stream(str(rules),str(Path(folder)/'state.db'),str(source),str(output))
        return result,[json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
    def test_late_quote_is_rejected_and_the_stream_continues(self):
        p=pk([{'feature':'water','op':'ge','value':200}])
        lines=[{'type':'history','events':[ou(0,5,95,market=0),ou(1,10,95,market=1)]},
               {'type':'quote','event':ou(2,7,95,market=0)},{'type':'quote','event':ou(3,11,95,market=1)}]
        with tempfile.TemporaryDirectory() as td:
            result,rows=self.run_stream(p,lines,td)
            self.assertEqual(result['rejected_messages'],1)
            self.assertEqual((rows[1]['status'],rows[1]['line']),('MESSAGE_REJECTED',2))
            self.assertEqual(rows[2]['status'],'PAPER_ONLY')
    def test_match_start_with_waiting_quotes_is_rejected_and_the_stream_continues(self):
        p=pk([{'feature':'water','op':'ge','value':100}])
        lines=[{'type':'quote','event':ou(0,1,95)},{'type':'match_start','sid':'a'},
               {'type':'history','events':[ou(0,1,95)]},{'type':'quote','event':ou(1,2,100)}]
        with tempfile.TemporaryDirectory() as td:
            result,rows=self.run_stream(p,lines,td)
            self.assertEqual(result['rejected_messages'],1)
            self.assertEqual((rows[0]['status'],rows[1]['status'],rows[1]['line']),('WAITING_HISTORY','MESSAGE_REJECTED',2))
            self.assertEqual(rows[3]['status'],'PAPER_ONLY')

if __name__=='__main__':unittest.main()
