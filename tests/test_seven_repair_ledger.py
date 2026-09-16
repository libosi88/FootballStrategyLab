"""Durable time boundaries and cancellation/settlement regressions (synthetic only)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.paper_execution import PaperDispatcher
from lab.paper_runner import StreamingPaperRunner
from test_core import event
from test_paper_execution import packet, offer


class DurableObservationRepair(unittest.TestCase):
    def test_live_flush_merges_pending_keys_into_one_decision_batch(self):
        p=packet(['LIVE_OVER','LIVE_GIVE'])
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'paper.db',p) as r:
            r.declare_new_match('a')
            r.feed({**event(0,line=8,ts=1),'market':0},_flush=False)
            r.feed(event(1,line=2,ts=2),_flush=False)
            result=r.flush(before=3,sid='a')
            self.assertEqual(len(result),2)
            self.assertTrue(all(item['status']=='RESERVED' for item in result))
            self.assertEqual(r.dispatcher.conn.execute('SELECT COUNT(*) FROM batches').fetchone()[0],1)
            self.assertEqual(r.pending,{})

    def test_merged_batch_uses_frozen_priority_for_shared_match_cap(self):
        p=packet(['LIVE_OVER','LIVE_GIVE'],cap=1)
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'paper.db',p) as r:
            r.declare_new_match('a')
            r.feed(event(0,line=2,ts=1),_flush=False)
            r.feed({**event(1,line=8,ts=2),'market':0},_flush=False)
            result={item['strategy_id']:item for item in r.flush(before=3,sid='a')}
            self.assertEqual(result['r0']['status'],'RESERVED')
            self.assertEqual(result['r1']['status'],'REJECTED')
            self.assertEqual(r.dispatcher.usage('a'),1)

    def test_alarm_failure_cannot_rewind_cross_market_clock(self):
        p=packet(['LIVE_OVER','LIVE_GIVE'])
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db'
            with StreamingPaperRunner(path,p) as runner:
                runner.declare_new_match('a')
                runner.feed({**event(0,line=8,ts=1),'market':0})
                with patch.object(runner.dispatcher,'alarm',side_effect=OSError('alarm failed')):
                    with self.assertRaises(OSError):runner.flush(before=100,sid='a')
            with StreamingPaperRunner(path,p) as runner:
                self.assertEqual(runner.watermarks['a'],100)
                with self.assertRaises(ValueError):runner.feed(event(1,ts=50))
                self.assertEqual(runner.dispatcher.conn.execute('SELECT COUNT(*) FROM intents').fetchone()[0],0)

    def test_checkpoint_failure_preserves_empty_and_nonempty_watermarks(self):
        for with_signal in (False,True):
            with self.subTest(with_signal=with_signal),tempfile.TemporaryDirectory() as td:
                p=packet(['LIVE_GIVE']);path=Path(td)/'paper.db'
                with StreamingPaperRunner(path,p,checkpoint_every=20) as runner:
                    runner.declare_new_match('a');runner.save(True)
                    if with_signal:runner.feed(event(0,ts=1));runner.save(True)
                    with patch.object(runner.dispatcher,'save_runner_state',side_effect=OSError('checkpoint failed')):
                        with self.assertRaises(OSError):runner.flush(before=100,sid='a');runner.save(True)
                with StreamingPaperRunner(path,p) as runner:
                    self.assertEqual(runner.watermarks['a'],100)
                    with self.assertRaises(ValueError):runner.feed(event(1,ts=50))

    def test_legacy_rejection_repairs_missing_ledger_clock(self):
        p=packet(['LIVE_OVER','LIVE_GIVE'])
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db'
            with StreamingPaperRunner(path,p) as runner:
                runner.declare_new_match('a');runner.feed({**event(0,line=8,ts=1),'market':0})
                saved=runner.dispatcher.load_runner_state('coordinator')
                runner.flush(before=100,sid='a')
            with PaperDispatcher(path,p) as d:
                d.save_runner_state('coordinator',saved)
                with d.conn:
                    d.conn.execute('DELETE FROM clocks')
                    d.conn.execute("DELETE FROM meta WHERE key='observed_clock_recovery_v2'")
            with StreamingPaperRunner(path,p) as runner:
                self.assertEqual(runner.watermarks['a'],100)
                with self.assertRaises(ValueError):runner.feed(event(1,ts=50))

    def test_rejection_and_clock_commit_together_and_never_regress(self):
        p=packet(['LIVE_OVER','LIVE_GIVE'])
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            first=offer(p,0,{**event(0,line=8,ts=1),'market':0})
            d.reject_unsubmitted([first],'stale',100)
            d.reject_unsubmitted([first],'retry',50)
            self.assertEqual(d.observed_clocks()['a'],100)
            with self.assertRaises(ValueError):d.submit_batch([offer(p,1,event(1,ts=50))])
            self.assertEqual(d.usage('a'),0)

    def test_watermark_validation_is_atomic(self):
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',packet(['LIVE_GIVE'])) as d:
            for invalid in (True,'100',1.5,None):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):d.record_watermarks({'a':100,'b':invalid})
                    self.assertEqual(d.observed_clocks(),{})
            d.record_watermarks({'a':100,'b':50});d.record_watermarks({'a':20})
            self.assertEqual(d.observed_clocks(),{'a':100,'b':50})

    def test_archive_replay_still_prices_prior_signals(self):
        p=packet(['LIVE_GIVE'])
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'paper.db',p,archive_history=True) as r:
            r.feed(event(0,ts=1));r.feed(event(1,ts=100));r.finish_match('a')
            rows=list(r.dispatcher.conn.execute('SELECT id FROM intents'))
            self.assertEqual(len(rows),1)
            self.assertEqual(r.dispatcher.intent(rows[0]['id'])['body']['execution_ts'],1)


class CancelledFillSettlementRepair(unittest.TestCase):
    def test_cancelled_fill_can_settle_after_restart_without_reopening(self):
        p=packet(['LIVE_GIVE'])
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db'
            with PaperDispatcher(path,p) as d:
                iid=d.submit_batch([offer(p,0,event(0,ts=1))])[0]['intent_id']
                d.receipt(iid,'part','PARTIAL','0.4')
                d.receipt(iid,'cancel','CANCELLED','0.4',True)
            with PaperDispatcher(path,p) as d:
                d.receipt(iid,'settle','SETTLED','0.4',True)
                d.receipt(iid,'settle','SETTLED','0.4',True)
                self.assertEqual(d.intent(iid)['status'],'SETTLED')
                self.assertEqual(d.intent(iid)['reserved'],0)
                self.assertEqual(d.usage('a'),0.4)
                with self.assertRaises(ValueError):d.receipt(iid,'reopen','PARTIAL','0.5')
                with self.assertRaises(ValueError):d.receipt(iid,'settle','SETTLED','0.5',True)

    def test_cancelled_fill_cannot_change_quantity_or_skip_confirmation(self):
        p=packet(['LIVE_GIVE'])
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            iid=d.submit_batch([offer(p,0,event(0,ts=1))])[0]['intent_id']
            d.receipt(iid,'part','PARTIAL','0.4');d.receipt(iid,'cancel','CANCELLED','0.4',True)
            for quantity,confirmed in [('0.5',True),('0.3',True),('0',True),('0.4',False)]:
                with self.subTest(quantity=quantity,confirmed=confirmed):
                    with self.assertRaises(ValueError):d.receipt(iid,'bad','SETTLED',quantity,confirmed)
                    self.assertEqual(d.intent(iid)['status'],'CANCELLED')
                    self.assertEqual(d.usage('a'),0.4)

    def test_cancelled_zero_fill_does_not_become_settleable(self):
        p=packet(['LIVE_GIVE'])
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            iid=d.submit_batch([offer(p,0,event(0,ts=1))])[0]['intent_id']
            d.receipt(iid,'cancel','CANCELLED','0',True)
            with self.assertRaises(ValueError):d.receipt(iid,'settle','SETTLED','0.4',True)
            self.assertEqual(d.usage('a'),0)


if __name__=='__main__':unittest.main()
